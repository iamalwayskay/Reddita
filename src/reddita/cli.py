"""Reddita — read-only Reddit research without an API key or login.

Usage:
  reddita search '<query>' [N]             site-wide post search
  reddita sub <subreddit> [N]              hot posts in a subreddit
  reddita subsearch <subreddit> '<q>' [N]  search within one subreddit
  reddita thread <permalink|id> [N]        post body + top N comments
  reddita about <subreddit>                community metadata

Flags:
  --json     full JSON output
  --fresh    bypass the 6h disk cache
  --new      sort by new instead of relevance/hot
  --top      sort by top (all-time) — search/sub only

The tool reads public Reddit feeds and community pages through an unauthenticated,
persistent, GET-only browser profile at ~/.local/share/reddita/browser-profile/.
Its private six-hour cache is at ~/.cache/reddita/. Posting, voting, messaging,
login, and private content are intentionally unsupported.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
from collections.abc import Callable

from .models import SourceBudgets, SourceRequest
from .scraper import RedditCaptureError, acquire_reddit
from .cache import canonical_digest, open_private_cache, record_rows


DEFAULT_CACHE = Path.home() / ".cache" / "reddita"
DEFAULT_PROFILE = Path.home() / ".local" / "share" / "reddita" / "browser-profile"


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
            "permalink": row.get("observedPermalink") or row.get("permalink") or "",
        } for kind, row in rows if kind == "post"]
    if operation == "thread":
        post = next((row for kind, row in rows if kind == "thread"), {})
        comments = [row for kind, row in rows if kind == "comment"]
        permalink = post.get("permalink") or ""
        requested_locator = capture.result.envelope.requested_locator
        if requested_locator.startswith("http"):
            permalink = requested_locator.replace("www.reddit.com", "old.reddit.com")
        return {
            "post": {
                "title": post.get("title"), "subreddit": post.get("subreddit") or "?",
                "score": post.get("score"), "author": post.get("author") or "?",
                "body": post.get("body") or "",
                "permalink": permalink,
            },
            "comments": [{
                "author": row.get("author") or "?", "score": row.get("score"),
                "body": row.get("body") or "",
            } for row in comments],
        }
    row = next((row for kind, row in rows if kind == "community"), {})
    return {
        "subreddit": f"r/{row.get('subreddit', '')}",
        "title": row.get("displayName") or "",
        "weekly_active_users": row.get("weeklyActiveUsers") or 0,
        "weekly_contributions": row.get("weeklyContributions") or 0,
        "description": row.get("description") or "",
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
) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    known_flags = {"--json", "--fresh", "--new", "--top"}
    supplied_flags = {argument for argument in raw if argument.startswith("--")}
    if supplied_flags - known_flags or {"--new", "--top"} <= supplied_flags:
        raise SystemExit(__doc__)
    json_output = "--json" in raw
    fresh = "--fresh" in raw
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

    if command == "search":
        operation, locator = "search", args[1]
        count = _count_argument(args, 2, 15, 100)
        parameters = {"pageSize": max(1, min(count, 25)), "maxComments": 100,
                      "sort": sort or "relevance"}
    elif command == "sub":
        operation, locator = "subreddit", _subreddit(args[1])
        count = _count_argument(args, 2, 15, 100)
        parameters = {"pageSize": max(1, min(count, 25)), "maxComments": 100,
                      "sort": sort or "hot"}
    elif command == "subsearch":
        operation, locator = "subreddit-search", _subreddit(args[1])
        count = _count_argument(args, 3, 15, 100)
        parameters = {"query": args[2], "pageSize": max(1, min(count, 25)),
                      "maxComments": 100, "sort": sort or "relevance"}
    elif command == "thread":
        operation, locator = "thread", args[1]
        count = _count_argument(args, 2, 20, 99)
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
    digest = canonical_digest({"schemaVersion": "reddita.v1", "request": request.model_dump(mode="json")})
    cache_fd = None if fresh else open_private_cache(cache_root)
    profile_fd = open_private_cache(profile_root)
    os.close(profile_fd)
    try:
        capture = (acquire or acquire_reddit)(
            request,
            budgets=SourceBudgets(
                timeout_seconds=300, max_requests=100,
                max_network_bytes=100_000_000, max_storage_bytes=1_000_000_000,
            ),
            settings_digest=digest, cache_fd=cache_fd,
            profile_root=profile_root, native_transport=True, allow_stealth=True,
        )
    except RedditCaptureError as error:
        raise SystemExit(str(error)) from None
    finally:
        if cache_fd is not None:
            os.close(cache_fd)

    envelope = capture.result.envelope
    if not envelope.complete:
        details = "; ".join(
            f"{gap.code}: {gap.detail}" for gap in envelope.gaps
        ) or "more bounded results are available"
        if not capture.result.records:
            raise SystemExit(f"Reddit capture incomplete — {details}")
        print(f"warning: Reddit capture is partial — {details}", file=sys.stderr)

    data = _legacy(capture, operation)
    if json_output:
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
        print(
            f"{data['weekly_active_users']:,} weekly active users · "
            f"{data['weekly_contributions']:,} weekly posts+comments"
        )
        print(data["description"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
