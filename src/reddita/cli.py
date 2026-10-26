"""Reddita — read-only Reddit research without Reddit developer credentials.

Usage:
  reddita search '<query>' [N]             site-wide post search
  reddita sub <subreddit> [N]              hot posts in a subreddit
  reddita subsearch <subreddit> '<q>' [N]  search within one subreddit
  reddita thread <permalink|id> [N]        post body + top N comments
  reddita about <subreddit>                community metadata
  reddita session login|status             dedicated account session
  reddita saved '<query>' [N]              search the local research corpus
  reddita export <file.jsonl> [N]           export saved records

Flags:
  --json       stable results-only JSON output
  --json-meta  JSON with completeness, gaps, provenance, and results
  --agent      compact, self-describing JSON for LLM/tool callers
  --fresh      bypass the 6h disk cache
  --mode MODE  auto, web-account, web-anon, or feed (default: auto)
  --save       add results to the private searchable research corpus
  --extended   allow up to 2,500 posts or 1,000 thread comments
  --new        sort by new instead of relevance/hot
  --top        sort by top (all-time) — search/sub only

The tool uses separate Reddita-owned anonymous and account browser profiles.
Account login is completed by the human in a headed browser; Reddita never accepts
credentials. Posting, voting, messaging, and private content are unsupported.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
from collections.abc import Callable

from .models import SourceBudgets, SourceRequest
from .scraper import RedditCaptureError
from .cache import canonical_digest, open_private_cache, record_rows
from .research import acquire_reddit_research
from .session import (
    AccessMode, default_profile_root, inspect_profile, login_account, profile_paths,
)


DEFAULT_CACHE = Path.home() / ".cache" / "reddita"
DEFAULT_PROFILE = default_profile_root()
DEFAULT_STORE = DEFAULT_PROFILE / "research.sqlite3"


def _count_argument(args: list[str], index: int, default: int, maximum: int) -> int:
    if len(args) <= index:
        return default
    raw = args[index]
    if not raw.isdigit() or not 1 <= int(raw) <= maximum:
        raise SystemExit(__doc__)
    return int(raw)


def _subreddit(value: str) -> str:
    return re.sub(r"^/?r/", "", value.strip()).strip("/").casefold()


def _legacy(capture, operation: str):
    rows = record_rows(capture)
    if operation in {"search", "subreddit", "subreddit-search"}:
        return [{
            "id": row.get("observedFullname") or row.get("id"),
            "subreddit": row.get("subreddit") or "?",
            "title": row.get("title"), "body": row.get("body") or "",
            "score": row.get("score"), "comments": row.get("commentCount"),
            "author": row.get("author") or "?", "age": row.get("age") or "",
            "url": row.get("externalUrl"),
            "permalink": row.get("permalink") or row.get("observedPermalink") or "",
        } for kind, row in rows if kind == "post"]
    if operation == "thread":
        post = next((row for kind, row in rows if kind == "thread"), {})
        comments = [row for kind, row in rows if kind == "comment"]
        permalink = post.get("permalink") or ""
        return {
            "post": {
                "id": post.get("id"), "age": post.get("age"),
                "title": post.get("title"), "subreddit": post.get("subreddit") or "?",
                "score": post.get("score"), "author": post.get("author") or "?",
                "body": post.get("body") or "",
                "permalink": permalink,
            },
            "comments": [{
                "id": row.get("id"), "age": row.get("age"),
                "author": row.get("author") or "?", "score": row.get("score"),
                "body": row.get("body") or "", "permalink": row.get("permalink"),
                "parentId": row.get("parentId"), "depth": row.get("depth"),
                "topLevel": row.get("topLevel"),
            } for row in comments],
        }
    row = next((row for kind, row in rows if kind == "community"), {})
    return {
        "subreddit": f"r/{row.get('subreddit', '')}",
        "title": row.get("displayName") or "",
        "weekly_active_users": row.get("weeklyActiveUsers"),
        "weekly_contributions": row.get("weeklyContributions"),
        "description": row.get("description") or "",
    }


def _json_document(capture, operation: str, requested_mode: str | None = None) -> dict:
    envelope = capture.result.envelope
    return {
        "complete": envelope.complete,
        "gaps": [gap.model_dump(mode="json") for gap in envelope.gaps],
        "continuation": envelope.continuation,
        "capturedAt": envelope.captured_at.isoformat(),
        "acquisitionMode": capture.acquisition_mode,
        "accessMode": capture.access_mode,
        "requestedMode": requested_mode,
        "transport": capture.transport,
        "sessionStatus": capture.session_status,
        "requestedLocator": envelope.requested_locator,
        "finalLocator": envelope.final_locator,
        "recordCount": envelope.record_count,
        "results": _legacy(capture, operation),
    }


def _agent_error(code: str, message: str) -> dict:
    retryable = code in {"rate-limited", "upstream-unavailable", "timeout", "profile-in-use"}
    return {
        "schemaVersion": "reddita.agent.v1",
        "ok": False,
        "error": {"code": code, "message": message, "retryable": retryable},
        "nextAction": "retry-later" if retryable else "inspect-error",
    }


def _print_posts(posts: list[dict]) -> None:
    for post in posts:
        score = "?" if post["score"] is None else str(post["score"])
        comments = "?" if post["comments"] is None else str(post["comments"])
        print(
            f"{score:>6}▲ {comments:>4}💬  "
            f"r/{post['subreddit']:<20} {post['title'][:78]}"
        )
        print(f"        {post['permalink']}")


def main(
    argv: list[str] | None = None, *, acquire: Callable | None = None,
    cache_root: Path = DEFAULT_CACHE,
    profile_root: Path = DEFAULT_PROFILE,
    store_path: Path = DEFAULT_STORE,
) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw in (["--help"], ["-h"]):
        print(__doc__)
        return 0
    if raw[:1] == ["session"]:
        if len(raw) != 2 or raw[1] not in {"login", "status"}:
            raise SystemExit(__doc__)
        paths = profile_paths(profile_root)
        if raw[1] == "login":
            try:
                state = login_account(paths[AccessMode.ACCOUNT])
            except Exception as error:
                raise SystemExit(f"Reddita could not open or verify the login browser: {error}") from None
            print(f"Logged in as u/{state.username} in Reddita's dedicated profile.")
            return 0
        try:
            state = inspect_profile(paths[AccessMode.ACCOUNT])
        except Exception as error:
            raise SystemExit(f"Reddita could not inspect the account profile: {error}") from None
        identity = f" as u/{state.username}" if state.username else ""
        print(f"account profile: {state.status.value}{identity}")
        return 0
    if raw[:1] in (["saved"], ["export"]):
        command = raw[0]
        if len(raw) not in {2, 3} or (len(raw) == 3 and not raw[2].isdigit()):
            raise SystemExit(__doc__)
        limit = 50 if len(raw) == 2 else int(raw[2])
        if not 1 <= limit <= 10_000:
            raise SystemExit(__doc__)
        from .storage import ResearchCorpus
        with ResearchCorpus(store_path) as corpus:
            if command == "export":
                count = corpus.export_jsonl(Path(raw[1]), limit=limit)
                print(f"Exported {count} records to {raw[1]}")
                return 0
            records = corpus.search(raw[1], limit=limit)
        documents = [{
            "source": item.source, "recordType": item.record_type,
            "recordId": item.record_id, "sourceLocator": item.source_locator,
            "data": item.data, "firstSeenAt": item.first_seen_at,
            "lastSeenAt": item.last_seen_at, "captureCount": item.capture_count,
        } for item in records]
        print(json.dumps(documents, indent=2))
        return 0
    if "--mode" in raw:
        position = raw.index("--mode")
        if position + 1 >= len(raw) or raw.count("--mode") != 1:
            raise SystemExit(__doc__)
        raw[position:position + 2] = [f"--mode={raw[position + 1]}"]
    mode_flags = [argument for argument in raw if argument.startswith("--mode=")]
    if len(mode_flags) > 1:
        raise SystemExit(__doc__)
    mode = mode_flags[0].split("=", 1)[1] if mode_flags else "auto"
    if mode not in {"auto", "web-account", "web-anon", "feed"}:
        raise SystemExit(__doc__)
    raw = [argument for argument in raw if not argument.startswith("--mode=")]
    known_flags = {
        "--json", "--json-meta", "--agent", "--fresh", "--new", "--top", "--save",
        "--extended",
    }
    supplied_flags = {argument for argument in raw if argument.startswith("--")}
    if (supplied_flags - known_flags or {"--new", "--top"} <= supplied_flags
            or len(supplied_flags & {"--json", "--json-meta", "--agent"}) > 1):
        raise SystemExit(__doc__)
    json_output = "--json" in raw
    json_meta_output = "--json-meta" in raw
    agent_output = "--agent" in raw
    fresh = "--fresh" in raw
    save = "--save" in raw
    extended = "--extended" in raw
    sort = "new" if "--new" in raw else "top" if "--top" in raw else None
    args = [argument for argument in raw if not argument.startswith("--")]
    if not args or args[0] not in {"search", "sub", "subsearch", "thread", "about"}:
        raise SystemExit(__doc__)
    command = args[0]
    allowed_lengths = {
        "search": {2, 3}, "sub": {2, 3}, "subsearch": {3, 4},
        "thread": {2, 3}, "about": {2},
    }
    if len(args) not in allowed_lengths[command]:
        raise SystemExit(__doc__)
    if command in {"thread", "about"} and supplied_flags & {"--new", "--top"}:
        raise SystemExit(__doc__)
    if command == "about" and extended:
        raise SystemExit(__doc__)

    if command == "search":
        operation, locator = "search", args[1]
        count = _count_argument(args, 2, 15, 2500 if extended else 100)
        parameters = {"pageSize": max(1, min(count, 25)), "maxComments": 100,
                      "sort": sort or "relevance"}
    elif command == "sub":
        operation, locator = "subreddit", _subreddit(args[1])
        count = _count_argument(args, 2, 15, 2500 if extended else 100)
        parameters = {"pageSize": max(1, min(count, 25)), "maxComments": 100,
                      "sort": sort or "hot"}
    elif command == "subsearch":
        operation, locator = "subreddit-search", _subreddit(args[1])
        count = _count_argument(args, 3, 15, 2500 if extended else 100)
        parameters = {"query": args[2], "pageSize": max(1, min(count, 25)),
                      "maxComments": 100, "sort": sort or "relevance"}
    elif command == "thread":
        operation, locator = "thread", args[1]
        count = _count_argument(args, 2, 20, 1000 if extended else 99)
        parameters = {"pageSize": max(1, min(count + 1, 100)),
                      "maxComments": min(count, 1000)}
    else:
        operation, locator, count = "about", _subreddit(args[1]), 1
        parameters = {"pageSize": 1, "maxComments": 100}

    request = SourceRequest(
        source="reddit", operation=operation, locator=locator,
        parameters_json=json.dumps(parameters, sort_keys=True, separators=(",", ":")),
        max_records=max(1, min(count + (1 if operation == "thread" else 0), 10_000)),
        max_pages=(1 if operation in {"thread", "about"} else max(1, (count + 24) // 25)),
    )
    digest = canonical_digest({
        "schemaVersion": "reddita.v2", "request": request.model_dump(mode="json"),
        "requestedMode": mode,
    })
    cache_fd = None if fresh else open_private_cache(cache_root)
    try:
        kwargs = dict(
            budgets=SourceBudgets(
                timeout_seconds=3600 if extended else 300,
                max_requests=1000 if extended else 100,
                max_network_bytes=100_000_000, max_storage_bytes=1_000_000_000,
            ),
            settings_digest=digest, cache_fd=cache_fd,
            profile_root=profile_root, native_transport=True, allow_stealth=True,
        )
        if acquire is None:
            kwargs["mode"] = mode
        capture = (acquire or acquire_reddit_research)(request, **kwargs)
    except RedditCaptureError as error:
        if agent_output:
            print(json.dumps(
                _agent_error(error.code, str(error)), separators=(",", ":"),
                ensure_ascii=False,
            ))
            return 2
        raise SystemExit(str(error)) from None
    finally:
        if isinstance(cache_fd, int):
            os.close(cache_fd)

    envelope = capture.result.envelope
    if not envelope.complete:
        details = "; ".join(
            f"{gap.code}: {gap.detail}" for gap in envelope.gaps
        ) or "more bounded results are available"
        if not capture.result.records:
            if agent_output:
                first_gap = envelope.gaps[0] if envelope.gaps else None
                document = _agent_error(
                    first_gap.code if first_gap else "incomplete",
                    first_gap.detail if first_gap else details,
                )
                print(json.dumps(document, separators=(",", ":"), ensure_ascii=False))
                return 2
            raise SystemExit(f"Reddit capture incomplete — {details}")
        if not agent_output:
            print(f"warning: Reddit capture is partial — {details}", file=sys.stderr)

    data = _legacy(capture, operation)
    if save:
        from .storage import ResearchCorpus
        with ResearchCorpus(store_path) as corpus:
            corpus.save_capture(capture, query_metadata={
                "operation": operation, "locator": locator,
                "parameters": parameters, "requestedMode": mode,
            })
    if agent_output:
        document = _json_document(capture, operation, mode)
        document = {
            "schemaVersion": "reddita.agent.v1", "ok": True,
            "nextAction": "use-results" if envelope.complete else "use-partial-results",
            **document,
        }
        print(json.dumps(document, separators=(",", ":"), ensure_ascii=False))
    elif json_meta_output:
        print(json.dumps(_json_document(capture, operation, mode), indent=2))
    elif json_output:
        print(json.dumps(data, indent=2))
    elif operation in {"search", "subreddit", "subreddit-search"}:
        _print_posts(data)
    elif operation == "thread":
        post = data["post"]
        post_score = "?" if post["score"] is None else str(post["score"])
        print(
            f"{post_score}▲ r/{post['subreddit']} — {post['title']}  "
            f"(u/{post['author']})"
        )
        if post["body"]:
            print("\n" + post["body"][:1500] + "\n" + "-" * 60)
        for comment in data["comments"]:
            score = "?" if comment["score"] is None else str(comment["score"])
            print(f"[{score}▲ u/{comment['author']}] {comment['body'][:500]}\n")
    else:
        print(f"{data['subreddit']} — {data['title']}")
        weekly_active = (
            "?" if data["weekly_active_users"] is None
            else f"{data['weekly_active_users']:,}"
        )
        weekly_contributions = (
            "?" if data["weekly_contributions"] is None
            else f"{data['weekly_contributions']:,}"
        )
        print(
            f"{weekly_active} weekly active users · "
            f"{weekly_contributions} weekly posts+comments"
        )
        print(data["description"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
