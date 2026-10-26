"""Normalize Reddit's read-only JSON surfaces into Reddita record rows."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urljoin, urlsplit


MAX_PAYLOAD_BYTES = 8 * 1024 * 1024
MAX_NODES = 10_000
MAX_DEPTH = 64
_THING_ID = re.compile(r"^(t[135])_([a-z0-9]{1,12})$")


class RedditJsonError(ValueError):
    """The response is not a bounded Reddit JSON document we can trust."""


def _document(payload: str | bytes | Mapping[str, Any] | list[Any]) -> Any:
    if isinstance(payload, bytes):
        if len(payload) > MAX_PAYLOAD_BYTES:
            raise RedditJsonError("Reddit JSON exceeds its byte limit")
        raw = payload.decode("utf-8")
    elif isinstance(payload, str):
        if len(payload.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise RedditJsonError("Reddit JSON exceeds its byte limit")
        raw = payload
    elif isinstance(payload, (Mapping, list)):
        return payload
    else:
        raise RedditJsonError("Reddit JSON has an unsupported payload type")
    try:
        return json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise RedditJsonError("Reddit returned malformed JSON") from error


def _mapping(value: Any, detail: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RedditJsonError(detail)
    return value


def _listing(
    value: Any, *, cursor_kinds: tuple[str, ...] = ("t3",),
) -> tuple[list[Any], str | None]:
    value = _mapping(value, "Reddit JSON listing is not an object")
    if value.get("kind") != "Listing":
        raise RedditJsonError("Reddit JSON listing has an invalid kind")
    data = _mapping(value.get("data"), "Reddit JSON listing has no data")
    children = data.get("children")
    if not isinstance(children, list) or len(children) > MAX_NODES:
        raise RedditJsonError("Reddit JSON listing children are invalid")
    after = data.get("after")
    cursor_pattern = rf"(?:{'|'.join(map(re.escape, cursor_kinds))})_[a-z0-9]{{1,12}}"
    if after is not None and (
        not isinstance(after, str) or re.fullmatch(cursor_pattern, after) is None
    ):
        raise RedditJsonError("Reddit JSON listing cursor is invalid")
    return children, after


def _thing(child: Any, expected_kind: str) -> tuple[str, Mapping[str, Any]]:
    child = _mapping(child, "Reddit JSON child is not an object")
    if child.get("kind") != expected_kind:
        raise RedditJsonError("Reddit JSON child has an unexpected kind")
    data = _mapping(child.get("data"), "Reddit JSON child has no data")
    identifier = data.get("id")
    fullname = data.get("name")
    if (not isinstance(identifier, str)
            or re.fullmatch(r"[a-z0-9]{1,12}", identifier) is None
            or fullname != f"{expected_kind}_{identifier}"):
        raise RedditJsonError("Reddit JSON child identifier is invalid")
    return identifier, data


def _absolute_permalink(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    absolute = urljoin("https://www.reddit.com/", value)
    try:
        parsed = urlsplit(absolute)
    except ValueError:
        return None
    if (parsed.scheme != "https" or parsed.hostname not in {"www.reddit.com", "old.reddit.com"}
            or parsed.username is not None or parsed.password is not None):
        return None
    return "https://www.reddit.com" + parsed.path + (
        f"?{parsed.query}" if parsed.query else ""
    )


def _external_url(value: Any, permalink: str | None, is_self: Any) -> str | None:
    if is_self is True or not isinstance(value, str) or not value:
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    if permalink is not None and parsed.hostname in {"www.reddit.com", "old.reddit.com"}:
        if parsed.path.rstrip("/") == urlsplit(permalink).path.rstrip("/"):
            return None
    return value


def _integer(value: Any) -> int | None:
    return value if type(value) is int else None


def _timestamp(value: Any) -> str | None:
    if type(value) not in {int, float}:
        return None
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


def _permalink_identity(value: str) -> tuple[str, str | None] | None:
    path = urlsplit(value).path.rstrip("/")
    match = re.search(r"/comments/([a-z0-9]{1,12})(?:/[^/]+)?(?:/(.*))?$", path)
    if match is None:
        return None
    remainder = match.group(2)
    comment_id = remainder.rsplit("/", 1)[-1] if remainder else None
    return match.group(1), comment_id


def _validate_post_permalink(identifier: str, permalink: str) -> None:
    identity = _permalink_identity(permalink)
    if identity is None or identity[0] != identifier:
        raise RedditJsonError("Reddit post permalink does not match its identifier")


def _validate_comment_permalink(thread_id: str, identifier: str, permalink: str) -> None:
    identity = _permalink_identity(permalink)
    if identity is None or identity != (thread_id, identifier):
        raise RedditJsonError("Reddit comment permalink does not match its identifier")


def _source_thread_id(source_url: str) -> str | None:
    match = re.search(r"/comments/([a-z0-9]{1,12})(?:/|$)", urlsplit(source_url).path)
    return match.group(1) if match is not None else None


def _post_row(data: Mapping[str, Any], source_url: str) -> dict[str, Any]:
    permalink = _absolute_permalink(data.get("permalink"))
    if permalink is None:
        raise RedditJsonError("Reddit JSON post permalink is invalid")
    title = data.get("title")
    if not isinstance(title, str) or not title.strip():
        raise RedditJsonError("Reddit JSON post title is invalid")
    identifier = data["id"]
    _validate_post_permalink(identifier, permalink)
    return {
        "age": _timestamp(data.get("created_utc")),
        "author": data.get("author") if isinstance(data.get("author"), str) else None,
        "body": data.get("selftext") if isinstance(data.get("selftext"), str) else None,
        "commentCount": _integer(data.get("num_comments")),
        "externalUrl": _external_url(data.get("url"), permalink, data.get("is_self")),
        "id": identifier,
        "observedFullname": data.get("name"),
        "observedPermalink": data.get("permalink"),
        "permalink": permalink,
        "score": _integer(data.get("score")),
        "subreddit": data.get("subreddit") if isinstance(data.get("subreddit"), str) else None,
        "surface": "structured",
        "title": title.strip(),
        "capturePage": source_url,
    }


def parse_listing_json(
    payload: str | bytes | Mapping[str, Any], source_url: str,
) -> tuple[list[tuple[str, str, dict[str, Any]]], str | None]:
    """Return normalized posts and Reddit's exact ``after`` fullname."""
    children, after = _listing(_document(payload))
    rows: list[tuple[str, str, dict[str, Any]]] = []
    seen: set[str] = set()
    for child in children:
        identifier, data = _thing(child, "t3")
        if identifier in seen:
            continue
        seen.add(identifier)
        rows.append((identifier, "post", _post_row(data, source_url)))
    return rows, after


def parse_thread_json(
    payload: str | bytes | list[Any], source_url: str, max_comments: int,
) -> tuple[list[tuple[str, str, dict[str, Any]]], bool]:
    """Return the thread and recursively flattened comments in Reddit order."""
    if type(max_comments) is not int or max_comments < 0:
        raise RedditJsonError("Reddit comment limit is invalid")
    document = _document(payload)
    if not isinstance(document, list) or len(document) < 2:
        raise RedditJsonError("Reddit thread JSON is not a two-listing document")
    post_children, _post_after = _listing(document[0])
    if len(post_children) != 1:
        raise RedditJsonError("Reddit thread JSON did not contain one requested post")
    thread_id, post_data = _thing(post_children[0], "t3")
    requested_thread = _source_thread_id(source_url)
    if requested_thread is not None and requested_thread != thread_id:
        raise RedditJsonError("Reddit thread JSON does not match its source URL")
    rows: list[tuple[str, str, dict[str, Any]]] = [
        (thread_id, "thread", _post_row(post_data, source_url)),
    ]
    comment_children, comment_after = _listing(
        document[1], cursor_kinds=("t1", "t3"),
    )
    emitted = 0
    visited = 0
    truncated = comment_after is not None
    seen: set[str] = set()

    def visit(children: list[Any], depth: int, inherited_parent: str) -> None:
        nonlocal emitted, visited, truncated
        if depth > MAX_DEPTH:
            raise RedditJsonError("Reddit comment tree exceeds its depth limit")
        for child in children:
            visited += 1
            if visited > MAX_NODES:
                raise RedditJsonError("Reddit comment tree exceeds its item limit")
            item = _mapping(child, "Reddit comment is not an object")
            if item.get("kind") == "more":
                data = _mapping(item.get("data"), "Reddit more-comments node has no data")
                if (_integer(data.get("count")) or 0) > 0 or data.get("children"):
                    truncated = True
                continue
            identifier, data = _thing(item, "t1")
            if identifier in seen:
                continue
            seen.add(identifier)
            parent_fullname = data.get("parent_id")
            match = _THING_ID.fullmatch(parent_fullname) if isinstance(parent_fullname, str) else None
            parent_id = match.group(2) if match is not None else inherited_parent
            body = data.get("body") if isinstance(data.get("body"), str) else None
            permalink = _absolute_permalink(data.get("permalink"))
            if body and permalink is not None:
                _validate_comment_permalink(thread_id, identifier, permalink)
                if emitted < max_comments:
                    rows.append((identifier, "comment", {
                        "age": _timestamp(data.get("created_utc")),
                        "author": data.get("author") if isinstance(data.get("author"), str) else None,
                        "body": body,
                        "depth": depth,
                        "id": identifier,
                        "parentId": parent_id,
                        "permalink": permalink,
                        "observedFullname": data.get("name"),
                        "score": _integer(data.get("score")),
                        "surface": "structured",
                        "topLevel": parent_fullname == f"t3_{thread_id}" if match else depth == 0,
                        "capturePage": source_url,
                    }))
                    emitted += 1
                else:
                    truncated = True
            replies = data.get("replies")
            if isinstance(replies, Mapping):
                reply_children, reply_after = _listing(replies)
                if reply_after is not None:
                    truncated = True
                visit(reply_children, depth + 1, identifier)
            elif replies is not None and replies != "":
                raise RedditJsonError("Reddit comment replies are invalid")

    visit(comment_children, 0, thread_id)
    declared_comments = _integer(post_data.get("num_comments"))
    if declared_comments is not None and declared_comments > len(seen):
        truncated = True
    return rows, truncated


def extract_more_children_json(
    payload: str | bytes | list[Any],
) -> tuple[str, ...]:
    """Extract bounded, validated comment IDs from recursive ``more`` nodes."""
    document = _document(payload)
    if not isinstance(document, list) or len(document) < 2:
        raise RedditJsonError("Reddit thread JSON is not a two-listing document")
    roots, _after = _listing(document[1], cursor_kinds=("t1", "t3"))
    result: list[str] = []
    seen: set[str] = set()
    visited = 0

    def visit(children: list[Any], depth: int) -> None:
        nonlocal visited
        if depth > MAX_DEPTH:
            raise RedditJsonError("Reddit comment tree exceeds its depth limit")
        for child in children:
            visited += 1
            if visited > MAX_NODES:
                raise RedditJsonError("Reddit comment tree exceeds its item limit")
            item = _mapping(child, "Reddit comment is not an object")
            kind = item.get("kind")
            data = _mapping(item.get("data"), "Reddit comment node has no data")
            if kind == "more":
                identifiers = data.get("children")
                if not isinstance(identifiers, list):
                    raise RedditJsonError("Reddit more-comments identifiers are invalid")
                for identifier in identifiers:
                    if (not isinstance(identifier, str)
                            or re.fullmatch(r"[a-z0-9]{1,12}", identifier) is None):
                        raise RedditJsonError("Reddit more-comments identifier is invalid")
                    if identifier not in seen:
                        if len(result) >= MAX_NODES:
                            raise RedditJsonError("Reddit more-comments list exceeds its item limit")
                        seen.add(identifier)
                        result.append(identifier)
                continue
            _identifier, comment = _thing(item, "t1")
            replies = comment.get("replies")
            if isinstance(replies, Mapping):
                nested, _nested_after = _listing(
                    replies, cursor_kinds=("t1", "t3"),
                )
                visit(nested, depth + 1)
            elif replies is not None and replies != "":
                raise RedditJsonError("Reddit comment replies are invalid")

    visit(roots, 0)
    return tuple(result)


def _comment_row(
    data: Mapping[str, Any], *, identifier: str, source_url: str,
    thread_id: str, depth: int | None,
) -> dict[str, Any]:
    parent_fullname = data.get("parent_id")
    parent_match = _THING_ID.fullmatch(parent_fullname) if isinstance(parent_fullname, str) else None
    if parent_match is None:
        raise RedditJsonError("Reddit comment parent identifier is invalid")
    parent_id = parent_match.group(2)
    body = data.get("body") if isinstance(data.get("body"), str) else None
    permalink = _absolute_permalink(data.get("permalink"))
    if not body or permalink is None:
        raise RedditJsonError("Reddit comment is incomplete")
    _validate_comment_permalink(thread_id, identifier, permalink)
    return {
        "age": _timestamp(data.get("created_utc")),
        "author": data.get("author") if isinstance(data.get("author"), str) else None,
        "body": body,
        "depth": depth,
        "id": identifier,
        "parentId": parent_id,
        "permalink": permalink,
        "observedFullname": data.get("name"),
        "score": _integer(data.get("score")),
        "surface": "structured",
        "topLevel": parent_fullname == f"t3_{thread_id}",
        "capturePage": source_url,
    }


def parse_morechildren_json(
    payload: str | bytes | Mapping[str, Any], source_url: str,
    thread_id: str, max_comments: int,
) -> tuple[list[tuple[str, str, dict[str, Any]]], bool]:
    """Normalize one bounded response from Reddit's read-only morechildren API."""
    if (re.fullmatch(r"[a-z0-9]{1,12}", thread_id) is None
            or type(max_comments) is not int or max_comments < 0):
        raise RedditJsonError("Reddit more-comments request is invalid")
    document = _mapping(_document(payload), "Reddit more-comments JSON is not an object")
    wrapper = _mapping(document.get("json"), "Reddit more-comments JSON has no wrapper")
    errors = wrapper.get("errors")
    if errors != []:
        raise RedditJsonError("Reddit more-comments response contains errors")
    data = _mapping(wrapper.get("data"), "Reddit more-comments JSON has no data")
    things = data.get("things")
    if not isinstance(things, list) or len(things) > MAX_NODES:
        raise RedditJsonError("Reddit more-comments rows are invalid")
    rows: list[tuple[str, str, dict[str, Any]]] = []
    seen: set[str] = set()
    truncated = False
    for child in things:
        item = _mapping(child, "Reddit more-comments row is not an object")
        if item.get("kind") == "more":
            truncated = True
            continue
        identifier, comment = _thing(item, "t1")
        if identifier in seen:
            continue
        seen.add(identifier)
        depth = _integer(comment.get("depth"))
        if depth is not None and depth < 0:
            raise RedditJsonError("Reddit comment depth is invalid")
        if len(rows) >= max_comments:
            truncated = True
            continue
        rows.append((identifier, "comment", _comment_row(
            comment, identifier=identifier, source_url=source_url,
            thread_id=thread_id, depth=depth,
        )))
    return rows, truncated


def extract_more_children_response_json(
    payload: str | bytes | Mapping[str, Any],
) -> tuple[str, ...]:
    """Return newly exposed child IDs from one morechildren response."""
    document = _mapping(_document(payload), "Reddit more-comments JSON is not an object")
    wrapper = _mapping(document.get("json"), "Reddit more-comments JSON has no wrapper")
    data = _mapping(wrapper.get("data"), "Reddit more-comments JSON has no data")
    things = data.get("things")
    if not isinstance(things, list) or len(things) > MAX_NODES:
        raise RedditJsonError("Reddit more-comments rows are invalid")
    result: list[str] = []
    seen: set[str] = set()
    for item in things:
        node = _mapping(item, "Reddit more-comments row is not an object")
        if node.get("kind") != "more":
            continue
        more = _mapping(node.get("data"), "Reddit more-comments node has no data")
        children = more.get("children")
        if not isinstance(children, list):
            raise RedditJsonError("Reddit more-comments identifiers are invalid")
        for identifier in children:
            if (not isinstance(identifier, str)
                    or re.fullmatch(r"[a-z0-9]{1,12}", identifier) is None):
                raise RedditJsonError("Reddit more-comments identifier is invalid")
            if identifier not in seen:
                seen.add(identifier)
                result.append(identifier)
    return tuple(result)


def parse_about_json(
    payload: str | bytes | Mapping[str, Any], source_url: str,
) -> list[tuple[str, str, dict[str, Any]]]:
    """Return normalized public community metadata."""
    document = _mapping(_document(payload), "Reddit about JSON is not an object")
    if document.get("kind") != "t5":
        raise RedditJsonError("Reddit about JSON has an invalid kind")
    data = _mapping(document.get("data"), "Reddit about JSON has no data")
    display_name = data.get("display_name")
    if not isinstance(display_name, str) or re.fullmatch(r"[A-Za-z0-9_]{2,21}", display_name) is None:
        raise RedditJsonError("Reddit about JSON has an invalid subreddit")
    source_match = re.search(r"/r/([A-Za-z0-9_]{2,21})(?:/|$)", urlsplit(source_url).path)
    if source_match is not None and source_match.group(1).casefold() != display_name.casefold():
        raise RedditJsonError("Reddit about JSON does not match its source URL")
    row = {
        "activeUserCount": _integer(data.get("accounts_active")),
        "capturePage": source_url,
        "description": data.get("public_description") if isinstance(data.get("public_description"), str) else None,
        "displayName": display_name,
        "observedFullname": data.get("name") if isinstance(data.get("name"), str) else None,
        "permalink": _absolute_permalink(data.get("url")),
        "subreddit": display_name,
        "subscriberCount": _integer(data.get("subscribers")),
        "surface": "structured",
        "title": data.get("title") if isinstance(data.get("title"), str) else None,
        "weeklyActiveUsers": None,
        "weeklyContributions": None,
    }
    return [(display_name.casefold(), "community", row)]
