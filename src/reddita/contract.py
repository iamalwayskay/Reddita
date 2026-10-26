"""Dependency-free executable contract for Reddit research requests."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from urllib.parse import unquote, urlencode, urlsplit


REDDIT_HOSTS = frozenset({"old.reddit.com", "www.reddit.com"})
SUBREDDIT_RE = re.compile(r"^[A-Za-z0-9_]{2,21}$")
REDDIT_ID_RE = re.compile(r"^[a-z0-9]{5,12}$")
REDDIT_OPERATIONS = frozenset({"search", "subreddit", "subreddit-search", "thread", "about"})


class RedditContractError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def parse_reddit_parameters_json(value: str) -> dict[str, object]:
    def unique_object(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise RedditContractError("invalid-parameters", "Reddit parameters contain duplicate keys")
            result[key] = item
        return result

    try:
        parameters = json.loads(value, object_pairs_hook=unique_object)
    except RedditContractError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise RedditContractError(
            "invalid-parameters", "Reddit parameters must be one strict JSON object",
        ) from error
    if not isinstance(parameters, dict) or set(parameters) - {
        "query", "sort", "pageSize", "maxComments",
    }:
        raise RedditContractError("invalid-parameters", "Reddit parameters contain unsupported fields")
    return parameters


def _query(value: object) -> str:
    if (type(value) is not str or value != value.strip() or not value
            or len(value.encode("utf-8")) > 512
            or any(ord(character) < 32 for character in value)):
        raise RedditContractError("invalid-locator", "Reddit query is invalid")
    return value


def _subreddit(value: str) -> str:
    raw = value.strip()
    if raw.startswith("r/"):
        raw = raw[2:]
    elif raw.startswith("/r/"):
        raw = raw[3:]
    raw = raw.rstrip("/")
    if (value != value.strip() or not SUBREDDIT_RE.fullmatch(raw)
            or "%" in value or "/" in raw or ".." in value):
        raise RedditContractError("invalid-locator", "Reddit subreddit is not canonical")
    return raw.casefold()


def canonical_reddit_thread_id(value: str) -> str:
    if "://" not in value:
        result = value.lower()
        if not REDDIT_ID_RE.fullmatch(result):
            raise RedditContractError("invalid-locator", "Reddit thread ID is invalid")
        return result
    try:
        parsed = urlsplit(value)
        decoded_path = unquote(parsed.path)
    except ValueError as error:
        raise RedditContractError("invalid-locator", "Reddit thread URL is invalid") from error
    if (parsed.scheme != "https" or parsed.hostname not in REDDIT_HOSTS
            or parsed.username is not None or parsed.password is not None or parsed.port is not None
            or parsed.query or parsed.fragment or decoded_path != parsed.path
            or ".." in decoded_path.split("/")):
        raise RedditContractError("invalid-locator", "Reddit thread URL is not canonical")
    match = re.fullmatch(
        r"/(?:r/[A-Za-z0-9_]{2,21}/)?comments/([a-z0-9]{5,12})(?:/[A-Za-z0-9_-]+)?/?",
        parsed.path,
    )
    if match is None:
        raise RedditContractError("invalid-locator", "Reddit thread URL path is invalid")
    return match.group(1)


def normalize_reddit_request(
    operation: str, locator: str, parameters: Mapping[str, object],
) -> tuple[str, str]:
    if operation not in REDDIT_OPERATIONS:
        raise RedditContractError("invalid-operation", "Reddit operation is invalid")
    if set(parameters) - {"query", "sort", "pageSize", "maxComments"}:
        raise RedditContractError("invalid-parameters", "Reddit parameters contain unsupported fields")
    page_size = parameters.get("pageSize", 25)
    max_comments = parameters.get("maxComments", 100)
    if (type(page_size) is not int or not 1 <= page_size <= 100
            or type(max_comments) is not int or not 0 <= max_comments <= 1000):
        raise RedditContractError("invalid-parameters", "Reddit parameter bounds are invalid")
    query = parameters.get("query")
    sort = parameters.get("sort")
    if sort is not None and type(sort) is not str:
        raise RedditContractError("invalid-parameters", "Reddit sort is invalid")

    if operation == "search":
        if query is not None or sort not in {None, "relevance", "new", "top"}:
            raise RedditContractError("invalid-parameters", "Reddit search parameters are incompatible")
        identity = _query(locator)
        pairs = [("q", identity)]
        if sort in {"new", "top"}:
            pairs.extend((("sort", sort), ("t", "all")))
        pairs.extend((("type", "link"), ("limit", str(min(page_size, 25)))))
        return identity, "https://www.reddit.com/search.rss?" + urlencode(pairs)

    if operation in {"subreddit", "subreddit-search", "about"}:
        identity = _subreddit(locator)
        if operation == "about":
            if query is not None or sort is not None:
                raise RedditContractError("invalid-parameters", "Reddit about parameters are incompatible")
            return identity, f"https://www.reddit.com/r/{identity}/"
        if operation == "subreddit-search":
            if sort not in {None, "relevance", "new", "top"}:
                raise RedditContractError("invalid-parameters", "Reddit search parameters are incompatible")
            canonical_query = _query(query)
            pairs = [("q", canonical_query), ("restrict_sr", "on")]
            if sort in {"new", "top"}:
                pairs.extend((("sort", sort), ("t", "all")))
            pairs.extend((("type", "link"), ("limit", str(min(page_size, 25)))))
            return identity, f"https://www.reddit.com/r/{identity}/search.rss?" + urlencode(pairs)
        if query is not None or sort not in {None, "hot", "new", "top"}:
            raise RedditContractError("invalid-parameters", "Reddit listing parameters are incompatible")
        canonical_sort = sort or "hot"
        pairs = []
        if canonical_sort == "top":
            pairs.append(("t", "all"))
        pairs.append(("limit", str(min(page_size, 25))))
        return identity, (
            f"https://www.reddit.com/r/{identity}/{canonical_sort}/.rss?"
            + urlencode(pairs)
        )

    if query is not None or sort is not None:
        raise RedditContractError("invalid-parameters", "Reddit thread parameters are incompatible")
    identity = canonical_reddit_thread_id(locator)
    limit = min(max_comments + 1, 100)
    return identity, f"https://www.reddit.com/comments/{identity}/.rss?limit={limit}"


def validate_reddit_request_json(operation: str, locator: str, parameters_json: str) -> tuple[str, str]:
    return normalize_reddit_request(operation, locator, parse_reddit_parameters_json(parameters_json))
