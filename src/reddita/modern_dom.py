"""Fallback parser for Reddit's modern standard-domain HTML elements."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup


class ModernDomError(ValueError):
    """Modern Reddit markup contained a result that could not be identified."""


def _number(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().replace(",", "")
    return int(normalized) if re.fullmatch(r"-?\d+", normalized) else None


def _identifier(node, prefix: str) -> str:
    candidates = (
        node.get("id"), node.get("thingid"), node.get("thing-id"), node.get("data-thingid"),
        node.get("post-id") if prefix == "t3" else node.get("comment-id"),
    )
    for value in candidates:
        if isinstance(value, str):
            match = re.fullmatch(rf"(?:{prefix}_)?([a-z0-9]{{1,12}})", value)
            if match is not None:
                return match.group(1)
    raise ModernDomError("Modern Reddit item identifier is invalid")


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


def _permalink_identity(value: str) -> tuple[str, str | None] | None:
    path = urlsplit(value).path.rstrip("/")
    match = re.search(r"/comments/([a-z0-9]{1,12})(?:/[^/]+)?(?:/(.*))?$", path)
    if match is None:
        return None
    remainder = match.group(2)
    return match.group(1), remainder.rsplit("/", 1)[-1] if remainder else None


def _source_thread_id(source_url: str) -> str | None:
    match = re.search(r"/comments/([a-z0-9]{1,12})(?:/|$)", urlsplit(source_url).path)
    return match.group(1) if match is not None else None


def _body(node, selectors: tuple[str, ...]) -> str | None:
    for selector in selectors:
        item = node.select_one(selector)
        if item is not None:
            value = item.get_text(" ", strip=True)
            if value:
                return value
    return None


def _post(node, source_url: str, record_type: str) -> tuple[str, str, dict[str, Any]]:
    identifier = _identifier(node, "t3")
    raw_permalink = node.get("permalink") or node.get("href")
    permalink = _absolute_permalink(raw_permalink)
    title = node.get("post-title") or node.get("title")
    if permalink is None or not isinstance(title, str) or not title.strip():
        raise ModernDomError("Modern Reddit post is incomplete")
    identity = _permalink_identity(permalink)
    if identity is None or identity[0] != identifier:
        raise ModernDomError("Modern Reddit post permalink does not match its identifier")
    content_href = node.get("content-href")
    external = None
    if isinstance(content_href, str) and content_href:
        try:
            destination = urlsplit(content_href)
        except ValueError:
            destination = None
        if (destination is not None and destination.scheme in {"http", "https"}
                and destination.hostname is not None and destination.username is None
                and destination.password is None
                and not (destination.hostname in {"www.reddit.com", "old.reddit.com"}
                         and destination.path.rstrip("/") == urlsplit(permalink).path.rstrip("/"))):
            external = content_href
    subreddit = node.get("subreddit-prefixed-name") or node.get("subreddit")
    if isinstance(subreddit, str) and subreddit.casefold().startswith("r/"):
        subreddit = subreddit[2:]
    return identifier, record_type, {
        "age": node.get("created-timestamp"),
        "author": node.get("author"),
        "body": _body(node, ("[slot='text-body'] .md", "[slot='text-body']")),
        "commentCount": _number(node.get("comment-count")),
        "externalUrl": external,
        "id": identifier,
        "observedFullname": f"t3_{identifier}",
        "observedPermalink": raw_permalink,
        "permalink": permalink,
        "score": _number(node.get("score")),
        "subreddit": subreddit if isinstance(subreddit, str) else None,
        "surface": "web",
        "title": title.strip(),
        "capturePage": source_url,
    }


def _tracker_post(node, source_url: str) -> tuple[str, str, dict[str, Any]]:
    identifier = _identifier(node, "t3")
    raw_context = node.get("data-faceplate-tracking-context")
    try:
        context = json.loads(raw_context) if isinstance(raw_context, str) else {}
    except json.JSONDecodeError as error:
        raise ModernDomError("Modern Reddit tracking context is invalid") from error
    post_context = context.get("post") if isinstance(context.get("post"), dict) else {}
    profile = context.get("profile") if isinstance(context.get("profile"), dict) else {}
    subreddit_context = context.get("subreddit") if isinstance(context.get("subreddit"), dict) else {}
    title_link = node.select_one("a[data-testid='post-title'][href]")
    title_node = node.select_one("[data-testid='post-title-text']")
    raw_permalink = title_link.get("href") if title_link is not None else None
    permalink = _absolute_permalink(raw_permalink)
    title = (
        title_node.get_text(" ", strip=True) if title_node is not None else
        title_link.get("aria-label") if title_link is not None else
        post_context.get("title")
    )
    if permalink is None or not isinstance(title, str) or not title.strip():
        raise ModernDomError("Modern Reddit search card is incomplete")
    identity = _permalink_identity(permalink)
    if identity is None or identity[0] != identifier:
        raise ModernDomError("Modern Reddit search permalink does not match its identifier")
    subreddit = subreddit_context.get("name")
    if isinstance(subreddit, str) and subreddit.casefold().startswith("r/"):
        subreddit = subreddit[2:]
    score_node = node.select_one("[data-testid='post-votes'] faceplate-number[number]")
    comments_node = node.select_one("[data-testid='post-comments'] faceplate-number[number]")
    for counter in node.select("faceplate-number[number]"):
        label = counter.parent.get_text(" ", strip=True).casefold()
        if score_node is None and "vote" in label:
            score_node = counter
        if comments_node is None and "comment" in label:
            comments_node = counter
    time_node = node.select_one("faceplate-timeago time[datetime]")
    return identifier, "post", {
        "age": time_node.get("datetime") if time_node is not None else None,
        "author": profile.get("name") if isinstance(profile.get("name"), str) else None,
        "body": _body(node, ("[data-testid='post-text']",)),
        "commentCount": _number(comments_node.get("number")) if comments_node else None,
        "externalUrl": None,
        "id": identifier,
        "observedFullname": f"t3_{identifier}",
        "observedPermalink": raw_permalink,
        "permalink": permalink,
        "score": _number(score_node.get("number")) if score_node else None,
        "subreddit": subreddit if isinstance(subreddit, str) else None,
        "surface": "web",
        "title": title.strip(),
        "capturePage": source_url,
    }


def parse_listing_dom(
    html: str, source_url: str,
) -> tuple[list[tuple[str, str, dict[str, Any]]], str | None]:
    """Return search/listing posts and the exact next-page fullname cursor."""
    soup = BeautifulSoup(html, "lxml")
    nodes = soup.select("search-telemetry-tracker shreddit-post")
    standalone_posts = [node for node in soup.select("shreddit-post") if node not in nodes]
    trackers = [
        node for node in soup.select(
            "search-telemetry-tracker[data-testid='search-sdui-post'][data-thingid]"
        ) if node.select_one("shreddit-post") is None
    ]
    rows: list[tuple[str, str, dict[str, Any]]] = []
    seen: set[str] = set()
    for node in [*nodes, *standalone_posts]:
        row = _post(node, source_url, "post")
        if row[0] not in seen:
            seen.add(row[0])
            rows.append(row)
    for node in trackers:
        row = _tracker_post(node, source_url)
        if row[0] not in seen:
            seen.add(row[0])
            rows.append(row)
    after = None
    next_link = soup.select_one("a[rel~='next'][href]")
    if next_link is not None:
        href = next_link.get("href")
        if isinstance(href, str):
            values = parse_qs(urlsplit(urljoin(source_url, href)).query).get("after", [])
            if len(values) == 1 and re.fullmatch(r"t3_[a-z0-9]{1,12}", values[0]):
                after = values[0]
    if after is None:
        partial = soup.select_one("faceplate-partial[src*='after=']")
        src = partial.get("src") if partial is not None else None
        if isinstance(src, str):
            values = parse_qs(urlsplit(urljoin(source_url, src)).query).get("after", [])
            if len(values) == 1 and re.fullmatch(r"t3_[a-z0-9]{1,12}", values[0]):
                after = values[0]
    return rows, after


def parse_thread_dom(
    html: str, source_url: str, max_comments: int,
) -> tuple[list[tuple[str, str, dict[str, Any]]], bool]:
    """Return a modern post and its visible ``shreddit-comment`` descendants."""
    if type(max_comments) is not int or max_comments < 0:
        raise ModernDomError("Modern Reddit comment limit is invalid")
    soup = BeautifulSoup(html, "lxml")
    post = soup.select_one("shreddit-post")
    if post is None:
        return [], False
    thread = _post(post, source_url, "thread")
    thread_id = thread[0]
    requested_thread = _source_thread_id(source_url)
    if requested_thread is not None and requested_thread != thread_id:
        raise ModernDomError("Modern Reddit thread does not match its source URL")
    rows: list[tuple[str, str, dict[str, Any]]] = [thread]
    nodes = soup.select("shreddit-comment")
    truncated = False
    seen: set[str] = set()
    for node in nodes:
        identifier = _identifier(node, "t1")
        if identifier in seen:
            continue
        seen.add(identifier)
        if len(rows) - 1 >= max_comments:
            truncated = True
            continue
        raw_parent = node.get("parent-id") or node.get("parentid")
        parent_match = re.fullmatch(r"t[13]_([a-z0-9]{1,12})", raw_parent or "")
        depth = _number(node.get("depth"))
        if depth is None:
            depth = sum(
                1 for ancestor in node.parents
                if getattr(ancestor, "name", None) == "shreddit-comment"
            )
        raw_permalink = node.get("permalink")
        permalink = _absolute_permalink(raw_permalink)
        body = _body(node, ("[slot='comment'] .md", "[slot='comment']"))
        if permalink is None or body is None:
            raise ModernDomError("Modern Reddit comment is incomplete")
        identity = _permalink_identity(permalink)
        if identity != (thread_id, identifier):
            raise ModernDomError("Modern Reddit comment permalink does not match its identifier")
        if (parent_match is not None and raw_parent.startswith("t3_")
                and parent_match.group(1) != thread_id):
            raise ModernDomError("Modern Reddit comment belongs to another thread")
        parent_id = parent_match.group(1) if parent_match is not None else (
            thread_id if depth == 0 else None
        )
        rows.append((identifier, "comment", {
            "age": node.get("created-timestamp"),
            "author": node.get("author"),
            "body": body,
            "depth": depth,
            "id": identifier,
            "parentId": parent_id,
            "permalink": permalink,
            "observedFullname": f"t1_{identifier}",
            "score": _number(node.get("score")),
            "surface": "web",
            "topLevel": raw_parent == f"t3_{thread_id}" if raw_parent else depth == 0,
            "capturePage": source_url,
        }))
    declared_comments = thread[2].get("commentCount")
    if isinstance(declared_comments, int) and declared_comments > len(seen):
        truncated = True
    return rows, truncated


def parse_about_dom(html: str, source_url: str) -> list[tuple[str, str, dict[str, Any]]]:
    """Return public metadata from the modern subreddit header."""
    soup = BeautifulSoup(html, "lxml")
    header = soup.select_one("shreddit-subreddit-header")
    if header is None:
        return []
    prefixed = header.get("prefixed-name") or header.get("subreddit-prefixed-name")
    if not isinstance(prefixed, str):
        raise ModernDomError("Modern subreddit header has no name")
    subreddit = prefixed[2:] if prefixed.casefold().startswith("r/") else prefixed
    if re.fullmatch(r"[A-Za-z0-9_]{2,21}", subreddit) is None:
        raise ModernDomError("Modern subreddit header name is invalid")
    source_match = re.search(r"/r/([A-Za-z0-9_]{2,21})(?:/|$)", urlsplit(source_url).path)
    if source_match is not None and source_match.group(1).casefold() != subreddit.casefold():
        raise ModernDomError("Modern subreddit header does not match its source URL")
    return [(subreddit.casefold(), "community", {
        "activeUserCount": _number(header.get("active-users")),
        "capturePage": source_url,
        "description": header.get("description"),
        "displayName": header.get("display-name") or subreddit,
        "permalink": f"https://www.reddit.com/r/{subreddit}/",
        "subreddit": subreddit,
        "subscriberCount": _number(header.get("subscribers")),
        "surface": "web",
        "weeklyActiveUsers": _number(header.get("weekly-active-users")),
        "weeklyContributions": _number(header.get("weekly-contributions")),
    })]
