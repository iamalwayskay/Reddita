"""Session-aware standard-domain Reddit acquisition and safe fallback orchestration."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .contract import canonical_reddit_thread_id, normalize_reddit_web_request
from .models import CaptureEnvelope, CaptureGap, CapturePage, CaptureRecord, SourceResult
from .modern_dom import parse_about_dom, parse_listing_dom, parse_thread_dom
from .reddit_json import (
    extract_more_children_json, extract_more_children_response_json,
    parse_about_json, parse_listing_json,
    parse_morechildren_json, parse_thread_json,
)
from .scraper import (
    MAX_HTML_BYTES, RedditCapture, RedditCaptureError, RedditParameters,
    _canonical_json, _open_browser_context, _result_digest, acquire_reddit,
)
from .session import (
    AccessMode, ProfileInUseError, ProfileLock, SessionState,
    classify_me_response, probe_session, profile_paths,
)


_SENSITIVE_PATH = re.compile(r"(?:^|/)(?:message|submit|account|settings|mod)(?:/|$)")


def _with_after(url: str, after: str | None) -> str:
    if not after:
        return url
    parsed = urlsplit(url)
    pairs = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
             if key not in {"after", "count"}]
    pairs.append(("after", after))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(pairs), ""))


def _safe_browser_resource(method: str, url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    if method != "GET" or parsed.scheme != "https" or parsed.username or parsed.password or parsed.port:
        return False
    if parsed.hostname == "www.redditstatic.com":
        return True
    return parsed.hostname == "www.reddit.com" and _SENSITIVE_PATH.search(parsed.path) is None


def _validate_landing(expected: str, actual: str) -> None:
    try:
        wanted, landed = urlsplit(expected), urlsplit(actual)
    except ValueError:
        raise RedditCaptureError("redirect-denied", f"Reddit returned an invalid URL (landed on {actual})") from None
    same_path = wanted.path.casefold() == landed.path.casefold()
    if wanted.path.startswith("/comments/"):
        try:
            same_path = canonical_reddit_thread_id(expected) == canonical_reddit_thread_id(actual)
        except Exception:
            same_path = False
    wanted_query = {
        key: value for key, value in parse_qsl(wanted.query, keep_blank_values=True)
        if key not in {"after", "count"}
    }
    landed_query = dict(parse_qsl(landed.query, keep_blank_values=True))
    same_query = all(landed_query.get(key) == value for key, value in wanted_query.items())
    if (landed.scheme != "https" or landed.hostname != "www.reddit.com"
            or not same_path or not same_query or _SENSITIVE_PATH.search(landed.path)):
        raise RedditCaptureError("redirect-denied", f"Reddit moved away from the requested page (landed on {actual})")


def _page_fetch(
    page, url: str, *, maximum_bytes: int = MAX_HTML_BYTES, timeout_ms: int = 30_000,
) -> tuple[int, str]:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "www.reddit.com":
        raise RedditCaptureError("request-denied", "Reddit web fetch must stay on the standard domain")
    relative = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    try:
        result = page.evaluate(
            """async ({url, maximum, timeout}) => {
              const controller = new AbortController();
              let timedOut = false;
              const timer = setTimeout(() => { timedOut = true; controller.abort(); }, timeout);
              try {
                const response = await fetch(url, {
                  credentials: 'include', headers: {accept: 'application/json'},
                  signal: controller.signal
                });
                if (!response.body) return {status: response.status, text: '', tooLarge: false};
                const reader = response.body.getReader();
                const chunks = [];
                let length = 0;
                while (true) {
                  const {done, value} = await reader.read();
                  if (done) break;
                  length += value.byteLength;
                  if (length > maximum) {
                    controller.abort();
                    return {status: response.status, text: '', tooLarge: true};
                  }
                  chunks.push(value);
                }
                const combined = new Uint8Array(length);
                let offset = 0;
                for (const chunk of chunks) { combined.set(chunk, offset); offset += chunk.byteLength; }
                return {status: response.status, text: new TextDecoder().decode(combined), tooLarge: false};
              } catch (error) {
                if (timedOut) return {status: 0, text: '', timedOut: true};
                throw error;
              } finally {
                clearTimeout(timer);
              }
            }""",
            {"url": relative, "maximum": maximum_bytes, "timeout": max(1, timeout_ms)},
        )
    except Exception as error:
        raise RedditCaptureError("browser-failed", "Reddit in-page fetch failed") from error
    if not isinstance(result, dict) or type(result.get("status")) is not int:
        raise RedditCaptureError("browser-failed", "Reddit returned an invalid in-page response")
    if result.get("timedOut"):
        raise RedditCaptureError("timeout", "Reddit in-page fetch exceeded its time budget")
    text = result.get("text")
    if result.get("tooLarge") or not isinstance(text, str) or len(text.encode("utf-8")) > maximum_bytes:
        raise RedditCaptureError("network-byte-limit", "Reddit in-page response exceeded its byte limit")
    return result["status"], text


def _stable_content(page) -> str:
    last_error: Exception | None = None
    for _attempt in range(4):
        try:
            return page.content()
        except Exception as error:
            last_error = error
            waiter = getattr(page, "wait_for_timeout", None)
            if callable(waiter):
                waiter(500)
            else:
                time.sleep(0.05)
    raise RedditCaptureError("browser-failed", "Reddit kept navigating before it could be read") from last_error


def _hydrate_listing(page, desired: int, check_budget=lambda: None) -> None:
    evaluate = getattr(page, "evaluate", None)
    waiter = getattr(page, "wait_for_timeout", None)
    locator = getattr(page, "locator", None)
    if not callable(evaluate) or not callable(waiter) or not callable(locator):
        return
    for _round in range(6):
        check_budget()
        count = locator("shreddit-post, [data-testid='search-sdui-post']").count()
        if count >= desired:
            return
        evaluate("window.scrollTo(0, document.body.scrollHeight)")
        waiter(600)
        check_budget()


def _parse_page(operation: str, payload: str, source_url: str, max_comments: int):
    if operation == "thread":
        return parse_thread_json(payload, source_url, max_comments=max_comments)
    if operation == "about":
        return parse_about_json(payload, source_url), False
    rows, after = parse_listing_json(payload, source_url)
    return rows, after


def _parse_dom(operation: str, html: str, source_url: str, max_comments: int):
    if operation == "thread":
        return parse_thread_dom(html, source_url, max_comments=max_comments)
    if operation == "about":
        return parse_about_dom(html, source_url), False
    return parse_listing_dom(html, source_url)


def _expand_more_comments(fetch, payload: str, thread_id: str, remaining: int):
    identifiers = list(extract_more_children_json(payload))
    if not identifiers:
        return [], True
    rows: list[tuple[str, str, dict]] = []
    seen_children = set(identifiers)
    incomplete = False
    for batch_index in range(10):
        if not identifiers or remaining <= 0:
            break
        batch, identifiers = identifiers[:100], identifiers[100:]
        url = "https://www.reddit.com/api/morechildren.json?" + urlencode({
            "api_type": "json", "link_id": f"t3_{thread_id}",
            "children": ",".join(batch), "raw_json": "1",
        })
        try:
            status, body = fetch(url)
            if status != 200:
                incomplete = True
                break
            expanded, truncated = parse_morechildren_json(
                body, url, thread_id, remaining,
            )
            newly_exposed = extract_more_children_response_json(body)
            for child_id in newly_exposed:
                if child_id not in seen_children:
                    seen_children.add(child_id)
                    identifiers.append(child_id)
        except RedditCaptureError as error:
            if error.code in {"timeout", "request-limit", "network-byte-limit", "execution-cancelled"}:
                raise
            incomplete = True
            break
        except Exception:
            incomplete = True
            break
        rows.extend(expanded)
        remaining -= len(expanded)
        incomplete = incomplete or (truncated and not newly_exposed)
        if batch_index == 9 and identifiers:
            incomplete = True
    return rows, incomplete or bool(identifiers)


def _capture_rank(capture: RedditCapture) -> tuple[int, int, int, int, int]:
    """Rank partial evidence by operation coverage, then transport fidelity."""
    operation = capture.result.envelope.operation.value
    wanted_type = "comment" if operation == "thread" else (
        "community" if operation == "about" else "post"
    )
    primary = sum(1 for record in capture.result.records if record.record_type == wanted_type)
    transport = 2 if capture.transport == "json" else 1 if capture.transport == "dom" else 0
    return (
        int(capture.result.envelope.complete), primary, len(capture.result.records),
        -len(capture.result.envelope.gaps), transport,
    )


def capture_web_context(
    request, context, *, mode: AccessMode, session: SessionState,
    captured_at: datetime | None = None, settings_digest: str | None = None,
    budgets=None, cancelled=None, ledger=None,
) -> RedditCapture:
    """Capture one request through an already-open owned browser context."""
    if mode not in {AccessMode.ACCOUNT, AccessMode.ANONYMOUS}:
        raise RedditCaptureError("invalid-mode", "A concrete web mode is required")
    if mode is AccessMode.ANONYMOUS and session.authenticated:
        raise RedditCaptureError(
            "anonymous-profile-authenticated",
            "The Reddita anonymous profile is logged in; use account mode or clear that profile",
        )
    try:
        parameters = RedditParameters.model_validate_json(request.parameters_json)
        identity, initial_page_url, initial_data_url = normalize_reddit_web_request(
            request.operation.value, request.locator,
            parameters.model_dump(mode="python", by_alias=True),
        )
    except Exception as error:
        if isinstance(error, RedditCaptureError):
            raise
        raise RedditCaptureError("invalid-parameters", "Reddit parameters are invalid") from error

    page = context.new_page()
    if ledger is None:
        ledger = {"started": time.monotonic(), "requests": 0, "bytes": 0}
    timeout_seconds = int(getattr(budgets, "timeout_seconds", 300))
    max_requests = int(getattr(budgets, "max_requests", 100))
    max_network_bytes = int(getattr(budgets, "max_network_bytes", 100_000_000))
    route_limit_hit = False

    def check_budget() -> None:
        if cancelled and cancelled():
            raise RedditCaptureError("execution-cancelled", "Reddit capture was cancelled")
        if time.monotonic() - ledger["started"] > timeout_seconds:
            raise RedditCaptureError("timeout", "Reddit web capture exceeded its time budget")
        if ledger["requests"] > max_requests:
            raise RedditCaptureError("request-limit", "Reddit web capture exceeded its request budget")
        if ledger["bytes"] > max_network_bytes:
            raise RedditCaptureError("network-byte-limit", "Reddit web capture exceeded its byte budget")

    def fetch(url: str) -> tuple[int, str]:
        check_budget()
        ledger["requests"] += 1
        remaining_bytes = max_network_bytes - ledger["bytes"]
        if remaining_bytes <= 0:
            raise RedditCaptureError("network-byte-limit", "Reddit web capture exceeded its byte budget")
        remaining_ms = max(1, int((timeout_seconds - (time.monotonic() - ledger["started"])) * 1000))
        status, body = _page_fetch(
            page, url, maximum_bytes=min(MAX_HTML_BYTES, remaining_bytes),
            timeout_ms=remaining_ms,
        )
        check_budget()
        return status, body

    route = getattr(page, "route", None)
    if callable(route):
        def readonly(candidate):
            nonlocal route_limit_hit
            resource_type = getattr(candidate.request, "resource_type", "")
            if resource_type in {"image", "media", "font"}:
                candidate.abort()
                return
            if _safe_browser_resource(candidate.request.method, candidate.request.url):
                ledger["requests"] += 1
                if ledger["requests"] > max_requests:
                    route_limit_hit = True
                    candidate.abort()
                    return
                candidate.continue_()
            else:
                candidate.abort()
        route("**/*", readonly)

    on = getattr(page, "on", None)
    if callable(on):
        def charge_finished_request(request):
            try:
                response = request.response()
                if response is not None:
                    ledger["bytes"] += len(response.body())
            except Exception:
                pass
        on("requestfinished", charge_finished_request)

    all_rows: list[tuple[str, str, dict]] = []
    pages: list[tuple[str, list[tuple[str, str, dict]]]] = []
    seen: set[tuple[str, str]] = set()
    after: str | None = None
    truncated = False
    used_transports: set[str] = set()
    for _page_index in range(request.max_pages):
        check_budget()
        page_url = _with_after(initial_page_url, after)
        remaining_ms = max(1, int((timeout_seconds - (time.monotonic() - ledger["started"])) * 1000))
        response = page.goto(page_url, wait_until="domcontentloaded", timeout=remaining_ms)
        waiter = getattr(page, "wait_for_timeout", None)
        if callable(waiter):
            waiter(750)
        if request.operation.value not in {"thread", "about"}:
            _hydrate_listing(page, min(request.max_records, parameters.page_size), check_budget)
        actual_url = getattr(page, "url", page_url)
        _validate_landing(page_url, actual_url)
        status = int(getattr(response, "status", 200) or 200)
        html = _stable_content(page)
        if route_limit_hit:
            raise RedditCaptureError("request-limit", "Reddit web capture exceeded its request budget")
        check_budget()
        if len(html.encode("utf-8")) > MAX_HTML_BYTES:
            raise RedditCaptureError("byte-limit", "Reddit page exceeds its byte limit")
        lowered = html.casefold()
        if status >= 400 or "prove your humanity" in lowered or "challenge-platform" in lowered:
            raise RedditCaptureError("bot-challenge", f"Reddit challenged the standard page (HTTP {status})")

        if _page_index == 0:
            observed_session = session
            try:
                me_status, me_body = fetch("https://www.reddit.com/api/me.json")
                observed_session = classify_me_response(me_status, me_body)
            except RedditCaptureError as error:
                if error.code in {"timeout", "request-limit", "network-byte-limit", "execution-cancelled"}:
                    raise
            if mode is AccessMode.ACCOUNT and not observed_session.authenticated:
                raise RedditCaptureError("account-not-verified", "The Reddita account profile is not logged in")
            if mode is AccessMode.ANONYMOUS and observed_session.authenticated:
                raise RedditCaptureError(
                    "anonymous-profile-authenticated",
                    "The Reddita anonymous profile is logged in; use account mode",
                )
            session = observed_session

        data_url = _with_after(initial_data_url, after)
        try:
            data_status, data_text = fetch(data_url)
        except RedditCaptureError as error:
            if error.code in {"timeout", "request-limit", "network-byte-limit", "execution-cancelled"}:
                raise
            data_status, data_text = 0, ""
        rows = []
        next_value = False
        if data_status == 200:
            try:
                rows, next_value = _parse_page(
                    request.operation.value, data_text, data_url, parameters.max_comments,
                )
                if request.operation.value == "thread" and next_value:
                    current_comments = sum(1 for _id, kind, _row in rows if kind == "comment")
                    expanded, still_incomplete = _expand_more_comments(
                        fetch, data_text, identity,
                        max(0, parameters.max_comments - current_comments),
                    )
                    rows.extend(expanded)
                    next_value = still_incomplete
                used_transports.add("json")
            except (ValueError, TypeError, json.JSONDecodeError):
                rows = []
        if not rows:
            try:
                rows, next_value = _parse_dom(
                    request.operation.value, html, actual_url, parameters.max_comments,
                )
            except (ValueError, TypeError):
                rows = []
            used_transports.add("dom")

        emitted: list[tuple[str, str, dict]] = []
        for identifier, record_type, row in rows:
            key = (record_type, identifier)
            if key in seen:
                continue
            seen.add(key)
            emitted.append((identifier, record_type, row))
            all_rows.append((identifier, record_type, row))
            if len(all_rows) >= request.max_records:
                truncated = True
                break
        pages.append((actual_url, emitted))
        if request.operation.value in {"thread", "about"}:
            truncated = truncated or bool(next_value)
            break
        after = str(next_value) if next_value else None
        if not after or len(all_rows) >= request.max_records:
            break

    if not all_rows:
        raise RedditCaptureError(
            "empty-results", f"Reddit returned no parseable {identity} records on the standard domain",
        )
    uncertain_dom = (
        request.operation.value not in {"thread", "about"}
        and "dom" in used_transports and len(all_rows) < request.max_records
        and after is None
    )
    more = truncated or after is not None
    gap = None
    if more:
        code = "comment-limit" if request.operation.value == "thread" else "record-limit"
        gap = CaptureGap(code=code, detail="Reddit web capture reached its configured limit", locator=pages[-1][0])
    elif uncertain_dom:
        gap = CaptureGap(
            code="coverage-unknown",
            detail="Reddit's rendered listing exposed fewer records than requested and no cursor",
            locator=pages[-1][0],
        )
    captured_at = captured_at or datetime.now(timezone.utc)
    records = tuple(CaptureRecord(
        record_id=f"reddit-{kind}-{identifier}", record_type=kind,
        source_locator=row.get("permalink") or pages[-1][0], source_index=index,
        data_json=_canonical_json(row),
    ) for index, (identifier, kind, row) in enumerate(all_rows))
    ids = {(record.record_type, record.record_id.rsplit("-", 1)[-1]): record.record_id for record in records}
    capture_pages = tuple(CapturePage(
        page_index=index, locator=url,
        record_ids=tuple(ids[(kind, identifier)] for identifier, kind, _row in page_rows),
    ) for index, (url, page_rows) in enumerate(pages))
    result = SourceResult(
        envelope=CaptureEnvelope(
            schema_version="1", source="reddit", operation=request.operation,
            requested_locator=request.locator, final_locator=pages[-1][0],
            captured_at=captured_at, collection_mode="rendered",
            complete=gap is None, record_count=len(records), continuation=None,
            gaps=() if gap is None else (gap,),
        ), records=records, pages=capture_pages,
    )
    transport = "dom" if "dom" in used_transports else "json"
    return RedditCapture(
        result=result, surface="live", content_digest=_result_digest(result),
        settings_digest=settings_digest, access_mode=(
            "web-account" if mode is AccessMode.ACCOUNT else "web-anon"
        ), transport=transport, session_status=session.status.value,
        original_acquired_at=captured_at,
    )


def acquire_reddit_research(
    request, *, budgets, settings_digest: str, mode: str = "auto",
    profile_root: Path | None = None, cache_fd: int | None = None,
    allow_stealth: bool = True, native_transport: bool = True, cancelled=None,
) -> RedditCapture:
    """Acquire with an explicit web session or an honest feed fallback."""
    aliases = {"web-account": AccessMode.ACCOUNT, "account": AccessMode.ACCOUNT,
               "web-anon": AccessMode.ANONYMOUS, "anonymous": AccessMode.ANONYMOUS}
    if mode == "feed":
        return acquire_reddit(
            request, budgets=budgets, settings_digest=settings_digest,
            profile_root=None,
            cache_fd=cache_fd, allow_stealth=allow_stealth,
            native_transport=native_transport, cancelled=cancelled,
        )
    if mode not in {"auto", *aliases}:
        raise RedditCaptureError("invalid-mode", "Reddit access mode is invalid")
    candidates = ([AccessMode.ACCOUNT, AccessMode.ANONYMOUS]
                  if mode == "auto" else [aliases[mode]])
    terminal_codes = {"timeout", "request-limit", "network-byte-limit", "execution-cancelled"}
    ledger = {"started": time.monotonic(), "requests": 0, "bytes": 0}
    last_error: RedditCaptureError | None = None
    best_web: RedditCapture | None = None
    paths = profile_paths(profile_root)
    from patchright.sync_api import sync_playwright
    for candidate in candidates:
        try:
            with ProfileLock(paths[candidate]), sync_playwright() as playwright:
                context, browser = _open_browser_context(
                    playwright, profile_root=paths[candidate], allow_stealth=allow_stealth,
                    channel="chrome" if candidate is AccessMode.ACCOUNT else None,
                )
                try:
                    state = probe_session(context)
                    web_capture = capture_web_context(
                        request, context, mode=candidate, session=state,
                        settings_digest=settings_digest, budgets=budgets,
                        cancelled=cancelled, ledger=ledger,
                    )
                    if mode != "auto" or web_capture.result.envelope.complete:
                        return web_capture
                    if best_web is None or _capture_rank(web_capture) > _capture_rank(best_web):
                        best_web = web_capture
                finally:
                    try:
                        context.close()
                    except Exception:
                        pass
                    if browser is not None:
                        try:
                            browser.close()
                        except Exception:
                            pass
        except ProfileInUseError as error:
            last_error = RedditCaptureError("profile-in-use", str(error))
            if mode != "auto":
                raise last_error from None
        except RedditCaptureError as error:
            last_error = error
            if mode != "auto" or error.code in terminal_codes:
                raise
        except Exception as error:
            last_error = RedditCaptureError(
                "browser-failed", f"Reddit browser mode failed: {type(error).__name__}",
            )
            if mode != "auto":
                raise last_error from None
    if mode == "auto":
        if cancelled and cancelled():
            raise RedditCaptureError("execution-cancelled", "Reddit capture was cancelled")
        elapsed = time.monotonic() - ledger["started"]
        remaining_seconds = int(getattr(budgets, "timeout_seconds", 300) - elapsed)
        remaining_requests = int(getattr(budgets, "max_requests", 100) - ledger["requests"])
        remaining_bytes = int(getattr(budgets, "max_network_bytes", 100_000_000) - ledger["bytes"])
        if remaining_seconds <= 0:
            raise RedditCaptureError("timeout", "Reddit capture exceeded its time budget")
        if remaining_requests <= 0:
            raise RedditCaptureError("request-limit", "Reddit capture exceeded its request budget")
        if remaining_bytes <= 0:
            raise RedditCaptureError("network-byte-limit", "Reddit capture exceeded its byte budget")
        feed_budgets = budgets.model_copy(update={
            "timeout_seconds": remaining_seconds,
            "max_requests": remaining_requests,
            "max_network_bytes": remaining_bytes,
        })
        feed_capture = acquire_reddit(
            request, budgets=feed_budgets, settings_digest=settings_digest,
            profile_root=None, cache_fd=cache_fd,
            allow_stealth=allow_stealth, native_transport=native_transport,
            cancelled=cancelled,
        )
        if (best_web is not None and not feed_capture.result.envelope.complete
                and _capture_rank(best_web) > _capture_rank(feed_capture)):
            return best_web
        return feed_capture
    assert last_error is not None
    raise last_error
