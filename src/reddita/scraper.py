"""Bounded Reddit research capture with an ephemeral, GET-only browser."""

from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import re
import stat
import subprocess
import sys
import time
from typing import Literal
from urllib.parse import parse_qsl, urlencode, unquote, urlsplit, urlunsplit
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup
from pydantic import AwareDatetime, Field, StrictInt

from .boundary import BoundaryModel
from .contract import (
    REDDIT_HOSTS,
    RedditContractError,
    canonical_reddit_thread_id,
    normalize_reddit_request,
)
from .models import (
    CaptureEnvelope, CaptureGap, CapturePage, CaptureRecord, SourceRequest,
    SourceResult, source_slice_digest,
)
from .transport import PublicHttpsBoundary, PublicTransportError


MAX_HTML_BYTES = 8 * 1024 * 1024
MAX_CONTINUATION_BYTES = 4096
_HOP_BY_HOP = frozenset({
    "authorization", "connection", "content-length", "cookie", "host", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "proxy-connection", "set-cookie", "te", "trailer",
    "transfer-encoding", "upgrade",
})
_HTTP_FIELD_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
class RedditCaptureError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class RedditNetworkBudget:
    def __init__(self, maximum: int) -> None:
        self.maximum = maximum
        self.used = 0

    @property
    def remaining(self) -> int:
        return self.maximum - self.used

    def add(self, payload: bytes) -> None:
        if len(payload) > self.remaining:
            raise RedditCaptureError("network-byte-limit", "Reddit exceeded its network byte budget")
        self.used += len(payload)


@contextmanager
def _open_response(browser_request, timeout_seconds: int, *, boundary=None):
    parsed = urlsplit(browser_request.url)
    boundary = boundary or PublicHttpsBoundary(allowed_hosts={parsed.hostname or ""})
    try:
        headers = {
            key: value for key, value in browser_request.all_headers().items()
            if key.lower() not in _HOP_BY_HOP
        }
        headers["accept-encoding"] = "identity"
        with boundary.open(
            browser_request.url, headers=headers, timeout_seconds=timeout_seconds,
        ) as response:
            yield response
    except PublicTransportError as error:
        raise RedditCaptureError(error.code, str(error)) from None


def _fulfill_reddit_route(
    route, budget: RedditNetworkBudget, timeout_seconds: int, *, boundary=None,
) -> None:
    try:
        with _open_response(route.request, timeout_seconds, boundary=boundary) as response:
            raw_headers = response.getheaders()
            nominated: set[str] = set()
            for name, value in raw_headers:
                if name.lower() != "connection":
                    continue
                for item in value.split(","):
                    token = item.strip(" \t")
                    if not token or _HTTP_FIELD_NAME.fullmatch(token) is None:
                        raise RedditCaptureError(
                            "response-header-unrepresentable",
                            "Reddit response contained an invalid Connection option",
                        )
                    nominated.add(token.lower())
            stripped = _HOP_BY_HOP | nominated
            lengths = [value.strip() for name, value in raw_headers
                       if name.lower() == "content-length"]
            transfers = [value for name, value in raw_headers
                         if name.lower() == "transfer-encoding"]
            if transfers and lengths:
                raise RedditCaptureError("network-byte-unmeasurable", "Reddit response length was ambiguous")
            declared = None
            if lengths:
                if (len(lengths) != 1 or not lengths[0].isdigit()
                        or str(int(lengths[0])) != lengths[0]):
                    raise RedditCaptureError("network-byte-unmeasurable", "Reddit response length was invalid")
                declared = int(lengths[0])
                if declared > budget.remaining:
                    raise RedditCaptureError("network-byte-limit", "Reddit exceeded its network byte budget")
            body = bytearray()
            while declared is None or len(body) < declared:
                size = min(65_536, budget.remaining + 1) if declared is None else min(
                    65_536, declared - len(body)
                )
                chunk = response.read(size)
                if not chunk:
                    break
                budget.add(chunk)
                body.extend(chunk)
            if declared is not None and len(body) != declared:
                raise RedditCaptureError("network-byte-unmeasurable", "Reddit response ended early")
            headers: dict[str, str] = {}
            names: set[str] = set()
            for name, value in raw_headers:
                lower = name.lower()
                if lower in stripped:
                    continue
                if lower in names:
                    raise RedditCaptureError(
                        "response-header-unrepresentable",
                        "Reddit response contained a repeated end-to-end header",
                    )
                names.add(lower)
                headers[name] = value
            headers["content-length"] = str(len(body))
            route.fulfill(status=response.status, headers=headers, body=bytes(body))
    except RedditCaptureError:
        raise
    except Exception:
        raise RedditCaptureError("network-byte-unmeasurable", "Reddit response bytes were not bounded") from None


class RedditParameters(BoundaryModel):
    query: str | None = Field(default=None, min_length=1, max_length=512)
    sort: Literal["relevance", "hot", "new", "top"] | None = None
    page_size: StrictInt = Field(default=25, ge=1, le=100)
    max_comments: StrictInt = Field(default=100, ge=0, le=1000)


class RedditPage(BoundaryModel):
    url: str = Field(min_length=1, max_length=8192)
    html: str = Field(max_length=MAX_HTML_BYTES)
    status: StrictInt = Field(default=200, ge=100, le=599)


class RedditCapture(BoundaryModel):
    result: SourceResult
    surface: Literal["archive", "live"]
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    settings_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    acquisition_mode: Literal["acquired", "cache-hit"] = "acquired"
    original_acquired_at: AwareDatetime


class RedditCache:
    """Private descriptor-rooted cache; malformed or unsafe entries are misses."""

    def __init__(self, root: Path, *, ttl_seconds: int = 21_600) -> None:
        self.root = Path(root)
        self._root_fd = None
        self.ttl_seconds = ttl_seconds
        info = self.root.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077 or self.root.is_symlink():
            raise RedditCaptureError("unsafe-cache", "Reddit cache root must be private")
        self._identity = (info.st_dev, info.st_ino)

    @classmethod
    def from_descriptor(cls, descriptor: int, *, ttl_seconds: int = 21_600) -> "RedditCache":
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
            raise RedditCaptureError("unsafe-cache", "Reddit cache descriptor must be private")
        instance = cls.__new__(cls)
        instance.root = None
        instance._root_fd = descriptor
        instance.ttl_seconds = ttl_seconds
        instance._identity = (info.st_dev, info.st_ino)
        return instance

    def _open(self) -> int:
        try:
            descriptor = (os.dup(self._root_fd) if self._root_fd is not None
                          else os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
        except OSError as error:
            raise RedditCaptureError("unsafe-cache", "Reddit cache root is unavailable") from error
        info = os.fstat(descriptor)
        if (not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077
                or (info.st_dev, info.st_ino) != self._identity):
            os.close(descriptor)
            raise RedditCaptureError("unsafe-cache", "Reddit cache root changed")
        return descriptor

    @staticmethod
    def _name(key: str) -> str:
        return hashlib.sha256(key.encode()).hexdigest() + ".json"

    def get(self, key: str, *, now: float | None = None) -> object | None:
        try:
            root_fd = self._open()
            try:
                descriptor = os.open(self._name(key), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                     dir_fd=root_fd)
                try:
                    before = os.fstat(descriptor)
                    if (not stat.S_ISREG(before.st_mode) or before.st_mode & 0o077
                            or before.st_size > 2_000_000):
                        return None
                    payload = os.pread(descriptor, before.st_size + 1, 0)
                    after = os.fstat(descriptor)
                    if (len(payload) != before.st_size
                            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
                        return None
                finally:
                    os.close(descriptor)
            finally:
                os.close(root_fd)
            value = json.loads(payload)
            current = time.time() if now is None else now
            if (set(value) != {"schemaVersion", "capturedAt", "keyHash", "data"}
                    or value["schemaVersion"] != "reddit-v2"
                    or value["keyHash"] != hashlib.sha256(key.encode()).hexdigest()
                    or type(value["capturedAt"]) not in {int, float}
                    or value["capturedAt"] > current
                    or current - value["capturedAt"] > self.ttl_seconds):
                return None
            return value["data"]
        except RedditCaptureError:
            raise
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def put(self, key: str, data: object, *, captured_at: float) -> None:
        payload = json.dumps({
            "schemaVersion": "reddit-v2", "capturedAt": captured_at,
            "keyHash": hashlib.sha256(key.encode()).hexdigest(), "data": data,
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        if len(payload) > 2_000_000:
            raise RedditCaptureError("cache-limit", "Reddit cache entry exceeds its byte limit")
        root_fd = self._open()
        temporary = f".{self._name(key)}.{os.getpid()}.{os.urandom(8).hex()}.tmp"
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=root_fd)
            try:
                offset = 0
                while offset < len(payload):
                    offset += os.write(descriptor, payload[offset:])
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.rename(temporary, self._name(key), src_dir_fd=root_fd, dst_dir_fd=root_fd)
            os.fsync(root_fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=root_fd)
            except FileNotFoundError:
                pass
            os.close(root_fd)


def _cached_capture_matches(
    capture: RedditCapture, request: SourceRequest, settings_digest: str,
) -> bool:
    try:
        parameters = RedditParameters.model_validate_json(request.parameters_json)
        _identity, initial_url = normalize_reddit_locator(
            request.operation.value, request.locator, parameters,
        )
        envelope = capture.result.envelope
        if envelope.final_locator is None:
            return False
        _validate_page_url(request.operation.value, _identity, initial_url,
                           envelope.final_locator)
        return (
            capture.acquisition_mode == "acquired"
            and capture.settings_digest == settings_digest
            and capture.content_digest == _result_digest(capture.result)
            and capture.surface == (
                "live" if request.operation.value == "about" else "archive"
            )
            and capture.original_acquired_at == envelope.captured_at
            and _cacheable_capture(capture)
            and envelope.source.value == "reddit"
            and envelope.operation is request.operation
            and envelope.requested_locator == request.locator
        )
    except Exception:
        return False


def _cacheable_capture(capture: RedditCapture) -> bool:
    envelope = capture.result.envelope
    return envelope.complete or (
        bool(capture.result.records)
        and bool(envelope.gaps)
        and all(gap.code in {"record-limit", "comment-limit"} for gap in envelope.gaps)
    )


def _write_cache_optional(
    cache, key: str, data: object, *, captured_at: float,
) -> None:
    try:
        cache.put(key, data, captured_at=captured_at)
    except RedditCaptureError as error:
        if error.code != "cache-limit":
            raise
    except OSError:
        pass


def _load_cached_capture(
    cache, key: str, request: SourceRequest, settings_digest: str,
) -> RedditCapture | None:
    cached = cache.get(key)
    if cached is None:
        return None
    try:
        capture = RedditCapture.model_validate(cached)
    except Exception:
        return None
    if not _cached_capture_matches(capture, request, settings_digest):
        return None
    return capture.model_copy(update={"acquisition_mode": "cache-hit"})


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _result_digest(result: SourceResult) -> str:
    return hashlib.sha256(result.model_dump_json().encode("utf-8")).hexdigest()


def normalize_reddit_locator(
    operation: str, locator: str, parameters: RedditParameters,
) -> tuple[str, str]:
    try:
        return normalize_reddit_request(
            operation, locator, parameters.model_dump(mode="python", by_alias=True),
        )
    except RedditContractError as error:
        raise RedditCaptureError(error.code, str(error)) from error


def allow_browser_request(method: str, url: str, *, request_count: int, max_requests: int) -> bool:
    try:
        parsed = urlsplit(url)
        decoded = unquote(parsed.path)
    except ValueError:
        return False
    return (
        method == "GET" and request_count <= max_requests and parsed.scheme == "https"
        and parsed.hostname in REDDIT_HOSTS and parsed.username is None
        and parsed.password is None and parsed.port is None and parsed.fragment == ""
        and decoded == parsed.path and ".." not in decoded.split("/")
        and not parsed.path.endswith(".json")
        and re.search(
            r"(?:^|/)(?:api|message|submit|login|account)(?:/|$)", parsed.path,
        ) is None
    )


def allow_browser_subresource(method: str, url: str) -> bool:
    """Allow only Reddit's inert static assets during rendered about capture."""
    try:
        parsed = urlsplit(url)
        decoded = unquote(parsed.path)
    except ValueError:
        return False
    return (
        method == "GET" and parsed.scheme == "https"
        and parsed.hostname == "www.redditstatic.com"
        and parsed.username is None and parsed.password is None
        and parsed.port is None and parsed.fragment == ""
        and decoded == parsed.path and ".." not in decoded.split("/")
    )


def _readonly_browser_route(route) -> None:
    """Let the persistent browser load resources without allowing write methods."""
    if route.request.method == "GET":
        route.continue_()
    else:
        route.abort()


def _number(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r"[\d,]+", value)
    return int(match.group().replace(",", "")) if match else None


def _text(node, selector: str) -> str | None:
    item = node.select_one(selector)
    if item is None:
        return None
    value = item.get_text(" ", strip=True)
    return value or None


def _canonical_permalink(value: str | None) -> str | None:
    if not value:
        return None
    if value.startswith("/"):
        value = "https://old.reddit.com" + value
    try:
        thread_id = canonical_reddit_thread_id(value)
    except RedditContractError:
        return None
    return f"https://www.reddit.com/comments/{thread_id}/"


def _observed_thread_id(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if (parsed.scheme != "https" or parsed.hostname not in REDDIT_HOSTS
            or parsed.username is not None or parsed.password is not None
            or parsed.port is not None or parsed.fragment):
        return None
    match = re.search(r"/comments/([a-z0-9]{5,12})(?:/|$)", parsed.path)
    return match.group(1) if match else None


def _observed_comment_matches(value: str | None, thread_id: str, comment_id: str) -> bool:
    if _observed_thread_id(value) != thread_id or value is None:
        return False
    path = urlsplit(value).path.rstrip("/")
    return re.search(
        rf"/comments/{re.escape(thread_id)}/[^/]+/{re.escape(comment_id)}$", path,
    ) is not None


def _invalid_feed(detail: str) -> None:
    raise RedditCaptureError("selector-drift", detail)


def _atom_author(entry) -> str | None:
    author = entry.find("author")
    name = author.find("name") if author is not None else None
    value = name.get_text(" ", strip=True) if name is not None else ""
    match = re.search(r"/u/([A-Za-z0-9_-]+)", value)
    return match.group(1) if match else None


def _atom_body(entry) -> str | None:
    content = entry.find("content")
    if content is None:
        return None
    fragment = BeautifulSoup(content.get_text(), "lxml")
    body = fragment.select_one("div.md")
    if body is None:
        return None
    value = body.get_text(" ", strip=True)
    return value or None


def _atom_link(entry) -> str | None:
    link = entry.find("link", recursive=False)
    value = link.get("href") if link is not None else None
    return value if isinstance(value, str) else None


def _atom_external_url(entry, thread_id: str) -> str | None:
    content = entry.find("content", recursive=False)
    if content is None:
        return None
    fragment = BeautifulSoup(content.get_text(), "lxml")
    for link in fragment.find_all("a", href=True):
        if link.get_text(" ", strip=True).casefold() != "[link]":
            continue
        value = link.get("href")
        if not isinstance(value, str):
            continue
        try:
            parsed = urlsplit(value)
        except ValueError:
            continue
        if (parsed.scheme not in {"http", "https"} or parsed.hostname is None
                or parsed.username is not None or parsed.password is not None):
            continue
        if _observed_thread_id(value) == thread_id:
            continue
        return value
    return None


def _rss_next_url(page_url: str, fullname: str, row_count: int) -> str | None:
    parsed = urlsplit(page_url)
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    limit = next((int(value) for key, value in pairs
                  if key == "limit" and value.isdigit()), 25)
    if row_count < limit:
        return None
    prior_count = next((int(value) for key, value in pairs
                        if key == "count" and value.isdigit()), 0)
    base = [(key, value) for key, value in pairs if key not in {"after", "count"}]
    base.extend((("after", fullname), ("count", str(prior_count + row_count))))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(base), ""))


def _rss_listing_rows(
    html: str, page_url: str,
) -> tuple[list[tuple[str, dict]], bool, str | None] | None:
    if re.search(r"<(?:[A-Za-z0-9_-]+:)?feed(?:\s|>)", html) is not None:
        try:
            ET.fromstring(html)
        except ET.ParseError:
            _invalid_feed("Reddit returned malformed Atom XML")
    soup = BeautifulSoup(html, "xml")
    feed = soup.find("feed")
    if feed is None:
        return None
    rows: list[tuple[str, dict]] = []
    last_fullname = None
    entries = feed.find_all("entry", recursive=False)
    for entry in entries:
        fullname_node = entry.find("id", recursive=False)
        fullname = fullname_node.get_text(strip=True) if fullname_node is not None else ""
        if re.fullmatch(r"t3_[a-z0-9]{5,12}", fullname) is None:
            _invalid_feed("Reddit returned an invalid Atom post identifier")
        raw_permalink = _atom_link(entry)
        permalink = _canonical_permalink(raw_permalink)
        title_node = entry.find("title", recursive=False)
        title = title_node.get_text(" ", strip=True) if title_node is not None else ""
        if permalink is None or not title:
            _invalid_feed("Reddit returned an incomplete Atom post")
        thread_id = fullname[3:]
        if canonical_reddit_thread_id(permalink) != thread_id:
            _invalid_feed("Reddit returned a mismatched Atom post")
        category = entry.find("category", recursive=False)
        updated = entry.find("updated", recursive=False)
        row = {
            "age": updated.get_text(strip=True) if updated is not None else None,
            "author": _atom_author(entry),
            "body": _atom_body(entry),
            "commentCount": None,
            "externalUrl": _atom_external_url(entry, thread_id),
            "id": thread_id,
            "observedFullname": fullname,
            "observedPermalink": raw_permalink,
            "permalink": permalink,
            "score": None,
            "subreddit": category.get("term") if category is not None else None,
            "surface": "syndication",
            "title": title,
            "capturePage": page_url,
        }
        rows.append((thread_id, row))
        last_fullname = fullname
    next_url = (
        _rss_next_url(page_url, last_fullname, len(rows))
        if last_fullname is not None else None
    )
    return rows, bool(entries), next_url


def _listing_rows(html: str, page_url: str) -> tuple[list[tuple[str, dict]], bool, str | None]:
    rss = _rss_listing_rows(html, page_url)
    if rss is not None:
        return rss
    if urlsplit(page_url).path.endswith(".rss"):
        _invalid_feed("Reddit feed response did not contain an Atom feed")
    soup = BeautifulSoup(html, "lxml")
    containers = soup.select("div.thing, div.search-result")
    rows: list[tuple[str, dict]] = []
    for item in containers:
        title = _text(item, "a.title") or _text(item, "a.search-title")
        raw_permalink = None
        comments = item.select_one("a.comments, a.search-comments")
        if comments is not None:
            raw_permalink = comments.get("href")
        link = item.select_one("a.title, a.search-title")
        if raw_permalink is None and link is not None:
            raw_permalink = link.get("href")
        permalink = _canonical_permalink(raw_permalink)
        if not title or permalink is None:
            continue
        thread_id = canonical_reddit_thread_id(permalink)
        fullname = item.get("data-fullname") or ""
        if fullname and fullname != f"t3_{thread_id}":
            continue
        subreddit_match = re.search(r"/r/([A-Za-z0-9_]{2,21})/", raw_permalink or "")
        subreddit = item.get("data-subreddit") or (
            subreddit_match.group(1) if subreddit_match else None
        )
        score_value = item.get("data-score")
        score = int(score_value) if score_value and score_value.isdigit() else _number(
            _text(item, "div.score.unvoted") or _text(item, "span.search-score")
        )
        author = item.get("data-author") or _text(item, "a.author")
        observed_permalink = (
            "https://old.reddit.com" + raw_permalink
            if raw_permalink and raw_permalink.startswith("/") else raw_permalink
        )
        row = {
            "author": author or None,
            "commentCount": _number(_text(item, "a.comments") or _text(item, "a.search-comments")),
            "externalUrl": link.get("href") if link is not None else None,
            "id": thread_id, "permalink": permalink,
            "observedFullname": fullname or None,
            "observedPermalink": observed_permalink,
            "score": score,
            "subreddit": subreddit, "surface": "archive", "title": title,
            "age": _text(item, "time") or _text(item, "span.search-time"),
            "capturePage": page_url,
        }
        rows.append((thread_id, row))
    next_node = soup.select_one("span.next-button a")
    next_url = next_node.get("href") if next_node is not None else None
    return rows, bool(containers), next_url


def _thread_rows(
    html: str, thread_id: str, page_url: str,
) -> tuple[list[tuple[str, str, dict]], bool, int]:
    if re.search(r"<(?:[A-Za-z0-9_-]+:)?feed(?:\s|>)", html) is not None:
        try:
            ET.fromstring(html)
        except ET.ParseError:
            _invalid_feed("Reddit returned malformed Atom XML")
    feed = BeautifulSoup(html, "xml").find("feed")
    if feed is not None:
        rows: list[tuple[str, str, dict]] = []
        comment_count = 0
        entries = feed.find_all("entry", recursive=False)
        if not entries:
            return [], False, 0
        first_id = entries[0].find("id", recursive=False)
        if first_id is None or first_id.get_text(strip=True) != f"t3_{thread_id}":
            _invalid_feed("Reddit thread feed did not begin with the requested post")
        for entry_index, entry in enumerate(entries):
            fullname_node = entry.find("id", recursive=False)
            fullname = fullname_node.get_text(strip=True) if fullname_node is not None else ""
            category = entry.find("category", recursive=False)
            raw_permalink = _atom_link(entry)
            updated = entry.find("updated", recursive=False)
            age = updated.get_text(strip=True) if updated is not None else None
            if entry_index == 0 and fullname == f"t3_{thread_id}":
                title_node = entry.find("title", recursive=False)
                if (title_node is None or not title_node.get_text(" ", strip=True)
                        or _observed_thread_id(raw_permalink) != thread_id):
                    _invalid_feed("Reddit returned an incomplete Atom thread")
                rows.append((thread_id, "thread", {
                    "age": age, "author": _atom_author(entry), "body": _atom_body(entry),
                    "id": thread_id,
                    "permalink": f"https://www.reddit.com/comments/{thread_id}/",
                    "observedFullname": fullname,
                    "observedPermalink": raw_permalink,
                    "score": None,
                    "subreddit": category.get("term") if category is not None else None,
                    "surface": "syndication",
                    "title": title_node.get_text(" ", strip=True) if title_node else None,
                    "capturePage": page_url,
                }))
            elif re.fullmatch(r"t1_[a-z0-9]{1,12}", fullname):
                body = _atom_body(entry)
                if (not body or raw_permalink is None
                        or not _observed_comment_matches(
                            raw_permalink, thread_id, fullname[3:],
                        )):
                    _invalid_feed("Reddit returned an incomplete Atom comment")
                comment_id = fullname[3:]
                rows.append((comment_id, "comment", {
                    "age": age, "author": _atom_author(entry), "body": body, "depth": None,
                    "id": comment_id, "parentId": None,
                    "permalink": raw_permalink or f"https://www.reddit.com/comments/{thread_id}/",
                    "observedFullname": fullname, "score": None,
                    "surface": "syndication", "topLevel": None,
                    "capturePage": page_url,
                }))
                comment_count += 1
            else:
                _invalid_feed("Reddit returned an invalid Atom thread entry")
        return rows, bool(rows), comment_count
    if urlsplit(page_url).path.endswith(".rss"):
        _invalid_feed("Reddit thread response did not contain an Atom feed")
    soup = BeautifulSoup(html, "lxml")
    top = soup.select_one("div.thing.link")
    rows: list[tuple[str, str, dict]] = []
    if top is not None:
        fullname = top.get("data-fullname")
        if fullname in {None, f"t3_{thread_id}"}:
            rows.append((thread_id, "thread", {
                "author": top.get("data-author") or _text(top, "a.author"),
                "body": _text(top, "div.expando div.md"), "id": thread_id,
                "permalink": f"https://old.reddit.com/comments/{thread_id}/",
                "observedFullname": fullname,
                "score": _number(top.get("data-score") or _text(top, "div.score.unvoted")),
                "subreddit": top.get("data-subreddit"), "surface": "archive",
                "title": _text(top, "a.title"), "capturePage": page_url,
            }))
    comments = soup.select("div.comment")
    for item in comments:
        fullname = item.get("data-fullname") or ""
        if not re.fullmatch(r"t1_[a-z0-9]{1,12}", fullname):
            continue
        comment_id = fullname[3:]
        body = _text(item, "div.md")
        if not body:
            continue
        raw_parent = item.get("data-parent-fullname")
        parent_id = raw_parent.split("_", 1)[1] if raw_parent and "_" in raw_parent else None
        depth_raw = item.get("data-depth")
        depth = int(depth_raw) if depth_raw and depth_raw.isdigit() else (
            0 if raw_parent == f"t3_{thread_id}" else None
        )
        rows.append((comment_id, "comment", {
            "author": item.get("data-author") or _text(item, "a.author"),
            "body": body, "depth": depth, "id": comment_id, "parentId": parent_id,
            "permalink": f"https://old.reddit.com/comments/{thread_id}/",
            "observedFullname": fullname,
            "score": _number(item.get("data-score") or _text(item, "span.score.unvoted")),
            "surface": "archive",
            "topLevel": (True if raw_parent == f"t3_{thread_id}" else
                         False if raw_parent is not None and raw_parent.startswith("t1_") else None),
            "capturePage": page_url,
        }))
    return rows, top is not None or bool(comments), len(comments)


def _about_rows(html: str, subreddit: str, page_url: str) -> tuple[list[tuple[str, dict]], bool]:
    soup = BeautifulSoup(html, "lxml")
    header = soup.select_one("shreddit-subreddit-header")
    if header is None:
        return [], False
    prefixed = header.get("prefixed-name") or f"r/{subreddit}"
    actual = prefixed[2:] if prefixed.startswith("r/") else prefixed
    if actual.casefold() != subreddit.casefold():
        raise RedditCaptureError(
            "redirect-denied",
            f"Reddit community metadata changed subreddit (landed on {page_url})",
        )
    row = {
        "description": (header.get("description") or "").strip() or None,
        "displayName": header.get("display-name") or None,
        "subreddit": subreddit, "surface": "live",
        "weeklyActiveUsers": _number(header.get("weekly-active-users")),
        "weeklyContributions": _number(header.get("weekly-contributions")),
    }
    return [(subreddit.casefold(), row)], True


def _request_digest(request: SourceRequest) -> str:
    # Slice allowances shrink while a corpus advances.  They constrain the
    # current acquisition but are not part of the source cursor's identity.
    payload = request.model_dump(
        mode="json", exclude={"continuation", "max_records", "max_pages"}
    )
    return hashlib.sha256(_canonical_json(payload).encode()).hexdigest()


def _continuation(
    request: SourceRequest, *, cursor_url: str, row_offset: int, source_index: int,
    seen_ids: tuple[str, ...],
) -> str:
    raw = json.dumps({
        "cursorUrl": cursor_url, "request": _request_digest(request),
        "rowOffset": row_offset, "seenIds": seen_ids, "sourceIndex": source_index,
    }, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _bounded_continuation(
    request: SourceRequest, *, cursor_url: str, row_offset: int, source_index: int,
    seen_ids: tuple[str, ...],
) -> str | None:
    token = _continuation(
        request, cursor_url=cursor_url, row_offset=row_offset,
        source_index=source_index, seen_ids=seen_ids,
    )
    return token if len(token.encode("utf-8")) <= MAX_CONTINUATION_BYTES else None


def _continuation_value(
    token: str | None, request: SourceRequest, initial_url: str,
) -> tuple[str, int, int, set[str]]:
    if token is None:
        return initial_url, 0, 0, set()
    try:
        raw = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
        value = json.loads(raw)
        cursor_url = value["cursorUrl"]
        row_offset = value["rowOffset"]
        seen_ids = value["seenIds"]
        source_index = value["sourceIndex"]
        if (set(value) != {"cursorUrl", "request", "rowOffset", "seenIds", "sourceIndex"}
                or type(cursor_url) is not str
                or type(row_offset) is not int or row_offset < 0
                or type(source_index) is not int or source_index < 0
                or type(seen_ids) is not list
                or any(type(item) is not str or not item for item in seen_ids)
                or seen_ids != sorted(set(seen_ids))
                or value["request"] != _request_digest(request)
                or _continuation(
                    request, cursor_url=cursor_url, row_offset=row_offset,
                    source_index=source_index, seen_ids=tuple(seen_ids),
                ) != token):
            raise ValueError
        if cursor_url == initial_url and row_offset == 0:
            raise ValueError
        if (cursor_url != initial_url
                and not any(key == "after" for key, _item in
                            parse_qsl(urlsplit(cursor_url).query, keep_blank_values=True))):
            raise ValueError
        return cursor_url, row_offset, source_index, set(seen_ids)
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        raise RedditCaptureError("invalid-continuation", "Reddit continuation does not match request") from None


def _validate_page_url(operation: str, identity: str, initial_url: str, page_url: str) -> None:
    try:
        parsed = urlsplit(page_url)
        initial = urlsplit(initial_url)
        left_origin = (
            parsed.scheme != "https" or parsed.hostname != initial.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.port is not None or parsed.fragment
            or unquote(parsed.path) != parsed.path or ".." in parsed.path.split("/")
        )
    except ValueError as error:
        raise RedditCaptureError(
            "redirect-denied", f"Reddit page URL is invalid (landed on {page_url})",
        ) from error
    if left_origin:
        raise RedditCaptureError(
            "redirect-denied", f"Reddit page left its allowed origin (landed on {page_url})",
        )
    if operation == "thread":
        if parsed.path != initial.path or parsed.query != initial.query:
            raise RedditCaptureError(
                "redirect-denied", f"Reddit page changed thread (landed on {page_url})",
            )
    elif operation == "about":
        if parsed.path.casefold() != f"/r/{identity}/".casefold() or parsed.query:
            raise RedditCaptureError(
                "redirect-denied", f"Reddit page changed subreddit (landed on {page_url})",
            )
    elif operation == "subreddit":
        expected = parse_qsl(initial.query, keep_blank_values=True)
        actual = parse_qsl(parsed.query, keep_blank_values=True)
        if (parsed.path != initial.path or actual[:len(expected)] != expected
                or urlencode(actual) != parsed.query
                or not _valid_cursor_pairs(actual[len(expected):])):
            raise RedditCaptureError(
                "redirect-denied",
                f"Reddit page changed subreddit listing (landed on {page_url})",
            )
    else:
        expected = parse_qsl(initial.query, keep_blank_values=True)
        actual = parse_qsl(parsed.query, keep_blank_values=True)
        if (parsed.path != initial.path or actual[:len(expected)] != expected
                or urlencode(actual) != parsed.query):
            raise RedditCaptureError(
                "redirect-denied", f"Reddit page changed search (landed on {page_url})",
            )
        if not _valid_cursor_pairs(actual[len(expected):]):
            raise RedditCaptureError(
                "redirect-denied",
                f"Reddit search added an unapproved parameter (landed on {page_url})",
            )


def _valid_cursor_pairs(pairs: list[tuple[str, str]]) -> bool:
    if not pairs:
        return True
    if len(pairs) not in {1, 2} or pairs[0][0] != "after":
        return False
    if re.fullmatch(r"t3_[a-z0-9]{5,12}", pairs[0][1]) is None:
        return False
    if len(pairs) == 2:
        count = pairs[1]
        if (count[0] != "count" or not count[1].isdigit()
                or str(int(count[1])) != count[1]):
            return False
    return True


def _wall_gap(page: RedditPage) -> CaptureGap | None:
    if page.status == 403:
        return CaptureGap(code="access-forbidden", detail="Reddit returned HTTP 403", locator=page.url)
    if page.status >= 400:
        return CaptureGap(code="http-error", detail=f"Reddit returned HTTP {page.status}", locator=page.url)
    lower = page.html.lower()
    if "whoa there" in lower or "you've been blocked" in lower or "robot check" in lower:
        return CaptureGap(code="bot-challenge", detail="Reddit returned an automated-access challenge", locator=page.url)
    return None


def capture_reddit_pages(
    request: SourceRequest, pages: tuple[RedditPage, ...], *, captured_at: datetime,
) -> RedditCapture:
    if request.source.value != "reddit":
        raise RedditCaptureError("invalid-operation", "Reddit capture requires a Reddit request")
    try:
        parameters = RedditParameters.model_validate_json(request.parameters_json)
    except Exception as error:
        raise RedditCaptureError("invalid-parameters", "Reddit parameters are invalid") from error
    identity, initial_url = normalize_reddit_locator(request.operation.value, request.locator, parameters)
    if len(pages) > request.max_pages:
        raise RedditCaptureError("page-limit", "Reddit returned unrequested pages")
    cursor_url, cursor_offset, source_index, prior_seen = _continuation_value(
        request.continuation, request, initial_url,
    )
    _validate_page_url(request.operation.value, identity, initial_url, cursor_url)
    page_chunks: list[
        tuple[str, list[tuple[str, str, dict]], str | None, bool]
    ] = []
    gap = None
    saw_container = False
    observed_thread_comments = 0
    final_url = cursor_url
    expected_url = cursor_url
    for page_index, page in enumerate(pages):
        if len(page.html.encode()) > MAX_HTML_BYTES:
            raise RedditCaptureError("byte-limit", "Reddit page exceeds its byte limit")
        _validate_page_url(request.operation.value, identity, initial_url, page.url)
        if page.url != expected_url and request.operation.value != "about":
            raise RedditCaptureError(
                "redirect-denied",
                f"Reddit pages are not a canonical cursor chain (landed on {page.url})",
            )
        final_url = page.url
        gap = _wall_gap(page)
        if gap:
            # The wall/error response was acquired and consumes the corpus page
            # ceiling even though it yielded no records.
            page_chunks.append((page.url, [], None, True))
            break
        next_url = None
        try:
            if request.operation.value == "thread":
                rows, contained, observed_thread_comments = _thread_rows(
                    page.html, identity, page.url,
                )
            elif request.operation.value == "about":
                about_rows, contained = _about_rows(page.html, identity, page.url)
                rows = [(item_id, "community", row) for item_id, row in about_rows]
            else:
                listing_rows, contained, next_url = _listing_rows(page.html, page.url)
                rows = [(item_id, "post", row) for item_id, row in listing_rows]
                if next_url is not None:
                    _validate_page_url(request.operation.value, identity, initial_url, next_url)
        except RedditCaptureError as error:
            if error.code != "selector-drift":
                raise
            gap = CaptureGap(code=error.code, detail=str(error), locator=page.url)
            page_chunks.append((page.url, [], None, True))
            saw_container = True
            break
        deduped_rows: list[tuple[str, str, dict]] = []
        seen: set[tuple[str, str]] = set()
        for identifier, record_type, row in rows:
            key = (record_type, identifier)
            if key not in seen:
                seen.add(key)
                deduped_rows.append((identifier, record_type, row))
        page_chunks.append((page.url, deduped_rows, next_url, False))
        saw_container = saw_container or contained
        if page_index + 1 < len(pages):
            if next_url is None:
                raise RedditCaptureError(
                    "redirect-denied",
                    f"Reddit returned an unrequested page (landed on {pages[page_index + 1].url})",
                )
            expected_url = next_url

    capacity = min(request.max_records, request.max_pages * parameters.page_size)
    if request.operation.value == "thread":
        capacity = min(capacity, parameters.max_comments + (1 if source_index == 0 else 0))
    selected: list[tuple[str, str, dict]] = []
    selected_by_page: list[tuple[str, list[tuple[str, str, dict]]]] = []
    next_cursor: tuple[str, int] | None = None
    remaining = capacity
    seen_ids = set(prior_seen)
    for page_index, (page_url, rows, next_url, is_gap_page) in enumerate(page_chunks):
        start = cursor_offset if page_index == 0 and not is_gap_page else 0
        if not is_gap_page and start > len(rows):
            raise RedditCaptureError("invalid-continuation", "Reddit continuation row is unavailable")
        emitted: list[tuple[str, str, dict]] = []
        position = start
        while position < len(rows):
            identifier, record_type, row = rows[position]
            key = f"{record_type}:{identifier}"
            if key in seen_ids:
                position += 1
                continue
            if remaining == 0:
                next_cursor = (page_url, position)
                break
            seen_ids.add(key)
            emitted.append((identifier, record_type, row))
            selected.append((identifier, record_type, row))
            remaining -= 1
            position += 1
        selected_by_page.append((page_url, emitted))
        if next_cursor is not None:
            break
        if remaining == 0:
            if page_index + 1 < len(page_chunks):
                next_cursor = (page_chunks[page_index + 1][0], 0)
            elif next_url is not None:
                next_cursor = (next_url, 0)
            break
        if page_index == len(page_chunks) - 1 and next_url is not None:
            next_cursor = (next_url, 0)

    if gap is not None:
        next_cursor = None
    if request.operation.value == "thread" and parameters.max_comments == 0:
        next_cursor = None
    continuation = None
    continuation_limited = False
    if next_cursor is not None:
        continuation = _bounded_continuation(
            request, cursor_url=next_cursor[0], row_offset=next_cursor[1],
            source_index=source_index + len(selected),
            seen_ids=tuple(sorted(seen_ids)),
        )
        if continuation is None:
            continuation_limited = True
        elif continuation == request.continuation:
            continuation = None
    thread_feed_saturated = request.operation.value == "thread" and (
        (parameters.max_comments == 0 and observed_thread_comments > 0)
        or (parameters.max_comments > 0 and observed_thread_comments
            >= min(parameters.max_comments, 99))
    )
    more = continuation is not None or thread_feed_saturated
    if gap is None and continuation_limited:
        gap = CaptureGap(
            code="continuation-limit",
            detail="Reddit exact continuation state exceeded its public byte limit",
            locator=final_url,
        )
    if gap is None and request.operation.value == "thread" and more:
        gap = CaptureGap(code="comment-limit", detail="Reddit capture reached its comment limit", locator=final_url)
    if gap is None and not selected:
        gap = CaptureGap(
            code="selector-drift" if saw_container else "empty-results",
            detail=("Reddit result containers no longer match supported selectors"
                    if saw_container else "Reddit returned no observable records"), locator=final_url,
        )
    if gap is None and more:
        gap = CaptureGap(code="record-limit", detail="Reddit capture reached its page or record limit", locator=final_url)
    records = tuple(CaptureRecord(
        record_id=f"reddit-{record_type}-{identifier}", record_type=record_type,
        source_locator=(row.get("permalink") or final_url), source_index=index,
        data_json=_canonical_json(row),
    ) for index, (identifier, record_type, row) in enumerate(selected, start=source_index))
    id_by_key = {
        (record_type, identifier): record.record_id
        for record, (identifier, record_type, _row) in zip(records, selected, strict=True)
    }
    selected_ids = set(id_by_key)
    emitted_by_locator = {
        page_url: [id_by_key[(record_type, identifier)]
                   for identifier, record_type, _row in page_rows
                   if (record_type, identifier) in selected_ids]
        for page_url, page_rows in selected_by_page
    }
    page_records = [
        (page_url, emitted_by_locator.get(page_url, []))
        for page_url, _rows, _next_url, _is_gap_page in page_chunks
    ]
    pages_out = tuple(CapturePage(
        page_index=index, locator=locator, record_ids=tuple(record_ids),
        continuation=continuation if index == len(page_records) - 1 else None,
    ) for index, (locator, record_ids) in enumerate(page_records))
    surface = "live" if request.operation.value == "about" else "archive"
    result = SourceResult(envelope=CaptureEnvelope(
            schema_version="1", source="reddit", operation=request.operation,
            requested_locator=request.locator, final_locator=final_url, captured_at=captured_at,
            collection_mode="rendered", complete=gap is None, record_count=len(records),
            continuation=continuation, gaps=() if gap is None else (gap,),
        ), records=records, pages=pages_out)
    return RedditCapture(
        result=result, surface=surface, content_digest=_result_digest(result),
        original_acquired_at=captured_at,
    )


def _matching_user_agent(playwright) -> str | None:
    """Return a Chrome user agent matching this OS and Patchright's Chromium."""
    try:
        executable = playwright.chromium.executable_path
        if not isinstance(executable, str) or not executable:
            return None
        output = subprocess.run(
            [executable, "--version"], capture_output=True, text=True, timeout=10,
        ).stdout
        match = re.search(r"(\d+)\.\d+\.\d+", output)
        if match is None:
            return None
        major = match.group(1)
    except Exception:
        return None
    if sys.platform == "darwin":
        os_part = "Macintosh; Intel Mac OS X 10_15_7"
    elif sys.platform.startswith("win"):
        os_part = "Windows NT 10.0; Win64; x64"
    else:
        os_part = f"X11; Linux {platform.machine() or 'x86_64'}"
    return (
        f"Mozilla/5.0 ({os_part}) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
    )


def _open_browser_context(playwright, *, profile_root: Path | None, allow_stealth: bool):
    """Open the original persistent Reddit browser, or an isolated API context."""
    launch_options = {"headless": True}
    if allow_stealth:
        launch_options["args"] = ["--disable-blink-features=AutomationControlled"]
    context_options = {
        "locale": "en-US",
        "timezone_id": "UTC",
        "viewport": {"width": 1366, "height": 900},
    }
    user_agent = _matching_user_agent(playwright)
    if user_agent is not None:
        context_options["user_agent"] = user_agent
    if profile_root is not None:
        context = playwright.chromium.launch_persistent_context(
            str(profile_root), **launch_options, **context_options,
        )
        return context, None
    browser = playwright.chromium.launch(**launch_options)
    context = browser.new_context(**context_options, java_script_enabled=False)
    return context, browser


def acquire_reddit(
    request: SourceRequest, *, budgets, settings_digest: str,
    cancelled=None, cache_fd: int | None = None,
    allow_stealth: bool = False, allow_active_interaction: bool = False,
    profile_root: Path | None = None, native_transport: bool = False,
) -> RedditCapture:
    # Reddit v1 performs no scripted interaction; carrying this independent
    # permission prevents a future interactive path from inheriting session or
    # stealth authority accidentally.
    _ = allow_active_interaction
    parameters = RedditParameters.model_validate_json(request.parameters_json)
    _identity, initial_url = normalize_reddit_locator(request.operation.value, request.locator, parameters)
    url, _row_offset, _source_index, _seen_ids = _continuation_value(
        request.continuation, request, initial_url,
    )
    _validate_page_url(request.operation.value, _identity, initial_url, url)
    cache_key = _canonical_json({
        "sourceSliceDigest": source_slice_digest(request),
        "settingsDigest": settings_digest,
    })
    cache = RedditCache.from_descriptor(cache_fd) if cache_fd is not None else None
    if cache is not None:
        cached = _load_cached_capture(cache, cache_key, request, settings_digest)
        if cached is not None:
            return cached
    pages: list[RedditPage] = []
    request_count = 0
    network_budget = RedditNetworkBudget(budgets.max_network_bytes)
    public_boundary = PublicHttpsBoundary(allowed_hosts=REDDIT_HOSTS)
    try:
        from patchright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            context, browser = _open_browser_context(
                playwright, profile_root=profile_root, allow_stealth=allow_stealth,
            )
            page = context.new_page()

            def route_request(route):
                nonlocal request_count
                is_navigation = getattr(route.request, "is_navigation_request", lambda: False)()
                if native_transport and not is_navigation:
                    if (request.operation.value != "about"
                            or not allow_browser_subresource(
                                route.request.method, route.request.url,
                            )):
                        route.abort()
                        return
                    request_count += 1
                    if request_count > budgets.max_requests:
                        route.abort()
                        return
                    route.continue_()
                    return
                if not allow_browser_request(
                    route.request.method, route.request.url,
                    request_count=1, max_requests=1,
                ):
                    route.abort()
                    return
                request_count += 1
                if request_count > budgets.max_requests:
                    route.abort()
                    return
                if is_navigation:
                    try:
                        _validate_page_url(request.operation.value, _identity, initial_url,
                                           route.request.url)
                    except RedditCaptureError:
                        route.abort()
                        raise
                if native_transport:
                    route.continue_()
                else:
                    _fulfill_reddit_route(
                        route, network_budget, budgets.timeout_seconds,
                        boundary=public_boundary,
                    )

            page.route("**/*", route_request)
            for _index in range(request.max_pages):
                if cancelled and cancelled():
                    raise RedditCaptureError("execution-cancelled", "Reddit capture was cancelled")
                wait_until = (
                    "networkidle"
                    if native_transport and request.operation.value == "about"
                    else "domcontentloaded"
                )
                response = page.goto(url, wait_until=wait_until,
                                     timeout=budgets.timeout_seconds * 1000)
                final_url = page.url
                html = (
                    page.content() if request.operation.value == "about"
                    else page.locator("body").inner_text()
                )
                status = response.status if response is not None else 200
                if native_transport:
                    network_budget.add(html.encode("utf-8"))
                pages.append(RedditPage(url=final_url, html=html, status=status))
                if status >= 400:
                    break
                if request.operation.value in {"thread", "about"}:
                    break
                _rows, _contained, next_url = _listing_rows(html, final_url)
                if not next_url:
                    break
                _validate_page_url(request.operation.value,
                                   normalize_reddit_locator(request.operation.value, request.locator, parameters)[0],
                                   initial_url, next_url)
                url = next_url
                time.sleep(random.uniform(1.0, 2.5))
            context.close()
            if browser is not None:
                browser.close()
    except RedditCaptureError:
        raise
    except Exception:
        raise RedditCaptureError("browser-failed", "Reddit browser capture failed") from None
    capture = capture_reddit_pages(request, tuple(pages), captured_at=datetime.now(timezone.utc))
    capture = capture.model_copy(update={"settings_digest": settings_digest})
    if cache is not None and _cacheable_capture(capture):
        _write_cache_optional(
            cache, cache_key, json.loads(capture.model_dump_json()),
            captured_at=capture.original_acquired_at.timestamp(),
        )
    return capture
