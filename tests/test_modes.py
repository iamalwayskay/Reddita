from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from contextlib import redirect_stdout
from io import StringIO
import json
import tempfile
import unittest
from unittest.mock import patch

from reddita.models import SourceBudgets, SourceRequest
from reddita.research import acquire_reddit_research, capture_web_context
from reddita.scraper import RedditCaptureError
from reddita.session import AccessMode, SessionState, SessionStatus
from reddita.cli import main
from reddita.scraper import RedditCapture


FIXTURES = Path(__file__).parent / "fixtures"


class _Response:
    def __init__(self, status: int, text: str) -> None:
        self.status = status
        self._text = text

    def text(self) -> str:
        return self._text


class _Request:
    def __init__(self, responses: dict[str, _Response]) -> None:
        self.responses = responses

    def get(self, url: str, **_kwargs):
        return self.responses[url]


class _Page:
    def __init__(self, html: str) -> None:
        self.url = "about:blank"
        self._html = html

    def goto(self, url: str, **_kwargs):
        self.url = url
        return _Response(200, "")

    def content(self) -> str:
        return self._html


class _EvaluatingPage(_Page):
    def __init__(self, html: str, responses: dict[str, dict]) -> None:
        super().__init__(html)
        self.responses = responses

    def evaluate(self, _script: str, argument=None):
        if argument is None:
            return None
        return self.responses[argument["url"]]


class _Context:
    def __init__(self, html: str, responses: dict[str, _Response]) -> None:
        self.request = _Request(responses)
        self._page = _Page(html)

    def new_page(self):
        return self._page


class _EvaluatingContext(_Context):
    def __init__(self, html: str, responses: dict[str, dict]) -> None:
        self.request = _Request({})
        self._page = _EvaluatingPage(html, responses)


def _request(operation: str = "search") -> SourceRequest:
    return SourceRequest(
        source="reddit", operation=operation, locator="repairable blender",
        parameters_json='{"maxComments":100,"pageSize":25,"sort":"relevance"}',
        max_records=25, max_pages=1,
    )


class ResearchModeTests(unittest.TestCase):
    def test_account_web_uses_in_page_same_origin_fetch_and_verifies_username(self):
        context = _EvaluatingContext("<html></html>", {
            "/api/me.json": {"status": 200, "text": '{"data":{"name":"researcher"}}', "length": 30},
            "/search.json?q=repairable+blender&type=link&limit=25&raw_json=1": {
                "status": 200,
                "text": (FIXTURES / "json-listing-search.json").read_text(),
                "length": 1000,
            },
        })
        capture = capture_web_context(
            _request(), context, mode=AccessMode.ACCOUNT,
            session=SessionState(SessionStatus.BLOCKED, http_status=403),
            captured_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(capture.access_mode, "web-account")
        self.assertEqual(capture.transport, "json")
        self.assertEqual(capture.session_status, "authenticated")
        self.assertEqual(capture.result.records[0].record_type, "post")

    def test_web_request_budget_is_enforced(self):
        context = _EvaluatingContext("<html></html>", {
            "/api/me.json": {"status": 200, "text": '{"data":null}', "length": 13},
            "/search.json?q=repairable+blender&type=link&limit=25&raw_json=1": {
                "status": 200, "text": "{}", "length": 2,
            },
        })
        with self.assertRaises(RedditCaptureError) as caught:
            capture_web_context(
                _request(), context, mode=AccessMode.ANONYMOUS,
                session=SessionState(SessionStatus.ANONYMOUS),
                budgets=SourceBudgets(max_requests=1),
            )
        self.assertEqual(caught.exception.code, "request-limit")

    def test_anonymous_standard_page_falls_back_from_blocked_json_to_modern_dom(self):
        data_url = (
            "https://www.reddit.com/search.json?q=repairable+blender&type=link"
            "&limit=25&raw_json=1"
        )
        context = _Context(
            (FIXTURES / "modern-search.html").read_text(),
            {data_url: _Response(403, "blocked")},
        )

        capture = capture_web_context(
            _request(), context, mode=AccessMode.ANONYMOUS,
            session=SessionState(SessionStatus.BLOCKED, http_status=403),
            captured_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

        self.assertEqual(capture.access_mode, "web-anon")
        self.assertEqual(capture.transport, "dom")
        self.assertEqual(capture.result.envelope.record_count, 2)
        self.assertEqual(capture.result.records[0].record_type, "post")

    def test_account_mode_requires_a_verified_username(self):
        context = _Context("<html></html>", {})
        with self.assertRaises(RedditCaptureError) as caught:
            capture_web_context(
                _request(), context, mode=AccessMode.ACCOUNT,
                session=SessionState(SessionStatus.BLOCKED, http_status=403),
                captured_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
        self.assertEqual(caught.exception.code, "account-not-verified")

    def test_anonymous_mode_refuses_an_authenticated_profile(self):
        context = _Context("<html></html>", {})
        with self.assertRaises(RedditCaptureError) as caught:
            capture_web_context(
                _request(), context, mode=AccessMode.ANONYMOUS,
                session=SessionState(SessionStatus.AUTHENTICATED, username="researcher"),
                captured_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
        self.assertEqual(caught.exception.code, "anonymous-profile-authenticated")

    def test_cli_passes_explicit_mode_and_can_save_search_and_export(self):
        fixture_context = _Context(
            (FIXTURES / "modern-search.html").read_text(),
            {(
                "https://www.reddit.com/search.json?q=repairable+blender&type=link"
                "&limit=2&raw_json=1"
            ): _Response(403, "blocked")},
        )
        capture = capture_web_context(
            SourceRequest(
                source="reddit", operation="search", locator="repairable blender",
                parameters_json='{"maxComments":100,"pageSize":2,"sort":"relevance"}',
                max_records=2, max_pages=1,
            ), fixture_context, mode=AccessMode.ANONYMOUS,
            session=SessionState(SessionStatus.BLOCKED, http_status=403),
            captured_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        observed = {}
        def acquire(_request, **kwargs) -> RedditCapture:
            observed.update(kwargs)
            return capture

        with tempfile.TemporaryDirectory() as directory:
            # macOS exposes /var as a system symlink; resolve the temporary
            # root because the cache intentionally rejects every symlink.
            root = Path(directory).resolve()
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(
                    ["search", "repairable blender", "2", "--json", "--save", "--mode=web-anon"],
                    acquire=acquire, cache_root=root / "cache", profile_root=root / "profiles",
                    store_path=root / "research.sqlite3",
                ), 0)
            saved = StringIO()
            with redirect_stdout(saved):
                self.assertEqual(main(
                    ["saved", "replaceable", "10"], store_path=root / "research.sqlite3",
                ), 0)
            documents = json.loads(saved.getvalue())
            self.assertEqual(len(documents), 1)
            exported = root / "export.jsonl"
            with redirect_stdout(StringIO()):
                self.assertEqual(main(
                    ["export", str(exported), "10"], store_path=root / "research.sqlite3",
                ), 0)
            self.assertEqual(len(exported.read_text().splitlines()), 2)
        self.assertNotIn("mode", observed)  # injected test acquire keeps the stable API

    def test_auto_falls_back_to_ephemeral_feed_after_browser_runtime_failure(self):
        sentinel = capture_web_context(
            _request(), _Context(
                (FIXTURES / "modern-search.html").read_text(),
                {"https://www.reddit.com/search.json?q=repairable+blender&type=link&limit=25&raw_json=1": _Response(403, "blocked")},
            ), mode=AccessMode.ANONYMOUS,
            session=SessionState(SessionStatus.BLOCKED, http_status=403),
            captured_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ).model_copy(update={"access_mode": "feed", "transport": "atom"})
        class PlaywrightManager:
            def __enter__(self): return object()
            def __exit__(self, *_args): return None
        with patch("reddita.research._open_browser_context", side_effect=RuntimeError("chrome failed")), patch(
            "patchright.sync_api.sync_playwright", return_value=PlaywrightManager(),
        ), patch("reddita.research.acquire_reddit", return_value=sentinel) as feed:
            result = acquire_reddit_research(
                _request(), budgets=SourceBudgets(), settings_digest="a" * 64,
                mode="auto",
            )
        self.assertIs(result, sentinel)
        self.assertIsNone(feed.call_args.kwargs["profile_root"])

    def test_auto_does_not_hide_a_terminal_budget_failure(self):
        class PlaywrightManager:
            def __enter__(self): return object()
            def __exit__(self, *_args): return None
        context = object()
        with patch(
            "reddita.research._open_browser_context", return_value=(context, None),
        ), patch(
            "reddita.research.probe_session",
            return_value=SessionState(SessionStatus.ANONYMOUS),
        ), patch(
            "reddita.research.capture_web_context",
            side_effect=RedditCaptureError("request-limit", "spent"),
        ), patch(
            "patchright.sync_api.sync_playwright", return_value=PlaywrightManager(),
        ), patch("reddita.research.acquire_reddit") as feed:
            with self.assertRaises(RedditCaptureError) as caught:
                acquire_reddit_research(
                    _request(), budgets=SourceBudgets(), settings_digest="a" * 64,
                    mode="auto",
                )
        self.assertEqual(caught.exception.code, "request-limit")
        feed.assert_not_called()


if __name__ == "__main__":
    unittest.main()
