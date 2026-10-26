from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from reddita import SourceBudgets, SourceRequest, SourceResult
from reddita.cache import open_private_cache
from reddita.cli import main
import reddita.cli as cli_module
from reddita.contract import (
    RedditContractError, normalize_reddit_request, normalize_reddit_web_request,
)
from reddita.models import CaptureEnvelope, CaptureGap
from reddita.scraper import RedditCaptureError, RedditPage, capture_reddit_pages
from reddita.scraper import allow_browser_request
from reddita.scraper import _validate_page_url
import reddita.scraper as scraper_module
from reddita.transport import PublicHttpsBoundary, PublicTransportError
from reddita.retry import retry_delay
import reddita.retry as retry_module


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
FIXTURES = Path(__file__).parent / "fixtures"


class RedditaTests(unittest.TestCase):
    def test_429_without_retry_after_uses_a_long_cooldown_not_server_jitter(self):
        with patch("reddita.retry.random.uniform", side_effect=lambda lower, _upper: lower) as jitter:
            self.assertEqual(retry_delay(429, None, 0), 10.0)
            self.assertEqual(retry_delay(429, None, 1), 20.0)
        self.assertEqual(jitter.call_args_list[0].args, (10.0, 15.0))
        self.assertEqual(jitter.call_args_list[1].args, (20.0, 30.0))

    def test_extended_feed_spacing_sustains_the_documented_unauthenticated_rate(self):
        with patch("reddita.retry.random.uniform", side_effect=lambda lower, _upper: lower):
            self.assertEqual(retry_module.request_spacing(extended=False), 1.0)
            self.assertEqual(retry_module.request_spacing(extended=True), 6.0)

    def test_feed_wait_is_deadline_and_cancellation_aware(self):
        clock = [0.0]
        def monotonic(): return clock[0]
        def sleep(seconds): clock[0] += seconds

        with self.assertRaises(RedditCaptureError) as timed_out:
            scraper_module._interruptible_wait(
                2.0, started=0.0, timeout_seconds=1,
                cancelled=None, monotonic=monotonic, sleep=sleep,
            )
        self.assertEqual(timed_out.exception.code, "timeout")

        clock[0] = 0.0
        with self.assertRaises(RedditCaptureError) as cancelled:
            scraper_module._interruptible_wait(
                2.0, started=0.0, timeout_seconds=10,
                cancelled=lambda: clock[0] >= 0.5,
                monotonic=monotonic, sleep=sleep,
            )
        self.assertEqual(cancelled.exception.code, "execution-cancelled")
        self.assertLess(clock[0], 2.0)

    def test_standard_web_contract_uses_normal_reddit_pages_and_json(self):
        cases = [
            ("search", "python tools", {"pageSize": 10, "maxComments": 100, "sort": "new"},
             "https://www.reddit.com/search/?q=python+tools&sort=new&t=all&type=link",
             "https://www.reddit.com/search.json?q=python+tools&sort=new&t=all&type=link&limit=10&raw_json=1"),
            ("subreddit", "Python", {"pageSize": 25, "maxComments": 100, "sort": "top"},
             "https://www.reddit.com/r/python/top/?t=all",
             "https://www.reddit.com/r/python/top.json?t=all&limit=25&raw_json=1"),
            ("subreddit-search", "Python", {"query": "typing", "pageSize": 25,
             "maxComments": 100, "sort": "relevance"},
             "https://www.reddit.com/r/python/search/?q=typing&restrict_sr=on&type=link",
             "https://www.reddit.com/r/python/search.json?q=typing&restrict_sr=on&type=link&limit=25&raw_json=1"),
            ("thread", "16fqhts", {"pageSize": 11, "maxComments": 10},
             "https://www.reddit.com/comments/16fqhts/",
             "https://www.reddit.com/comments/16fqhts/.json?limit=10&raw_json=1"),
        ]
        for operation, locator, parameters, page_url, json_url in cases:
            with self.subTest(operation=operation):
                identity, actual_page, actual_json = normalize_reddit_web_request(
                    operation, locator, parameters,
                )
                self.assertTrue(identity)
                self.assertEqual(actual_page, page_url)
                self.assertEqual(actual_json, json_url)

    def test_search_contract_is_canonical_and_bounded(self):
        identity, url = normalize_reddit_request(
            "search", "python tools", {"pageSize": 10, "maxComments": 100},
        )
        self.assertEqual(identity, "python tools")
        self.assertEqual(
            url,
            "https://www.reddit.com/search.rss?q=python+tools&type=link&limit=10",
        )
        with self.assertRaises(RedditContractError):
            normalize_reddit_request(
                "search", "python", {"pageSize": 101, "maxComments": 100},
            )

    def test_subreddit_contract_uses_public_syndication_pages(self):
        identity, url = normalize_reddit_request(
            "subreddit", "BuyItForLife",
            {"pageSize": 100, "maxComments": 100, "sort": "top"},
        )
        self.assertEqual(identity, "buyitforlife")
        self.assertEqual(
            url,
            "https://www.reddit.com/r/buyitforlife/top/.rss?t=all&limit=25",
        )

    def test_search_feed_returns_research_text_and_exact_continuation(self):
        request = SourceRequest(
            source="reddit", operation="search", locator="best office chair back pain",
            parameters_json='{"maxComments":100,"pageSize":2,"sort":"relevance"}',
            max_records=4, max_pages=2,
        )
        page_url = (
            "https://www.reddit.com/search.rss?q=best+office+chair+back+pain"
            "&type=link&limit=2"
        )
        capture = capture_reddit_pages(
            request,
            (RedditPage(url=page_url, html=(FIXTURES / "search.rss").read_text(encoding="utf-8")),),
            captured_at=NOW,
        )
        rows = [json.loads(record.data_json) for record in capture.result.records]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["id"], "1x3uyel")
        self.assertEqual(rows[0]["author"], "ChenJiaYi_804")
        self.assertEqual(rows[0]["subreddit"], "techIndia")
        self.assertIn("dining chair + random cushion", rows[0]["body"])
        self.assertIsNone(rows[0]["score"])
        self.assertIsNone(rows[0]["commentCount"])
        self.assertIsNotNone(capture.result.envelope.continuation)

    def test_link_post_preserves_its_external_destination(self):
        request = SourceRequest(
            source="reddit", operation="search", locator="durable product",
            parameters_json='{"maxComments":100,"pageSize":25,"sort":"relevance"}',
            max_records=25, max_pages=1,
        )
        capture = capture_reddit_pages(
            request,
            (RedditPage(
                url=("https://www.reddit.com/search.rss?q=durable+product"
                     "&type=link&limit=25"),
                html=(FIXTURES / "external-link.rss").read_text(encoding="utf-8"),
            ),),
            captured_at=NOW,
        )
        row = json.loads(capture.result.records[0].data_json)
        self.assertEqual(row["externalUrl"], "https://example.com/products/durable-chair")
        self.assertEqual(row["permalink"], "https://www.reddit.com/comments/abc123/")

    def test_invalid_or_truncated_search_feed_fails_closed(self):
        request = SourceRequest(
            source="reddit", operation="search", locator="best office chair back pain",
            parameters_json='{"maxComments":100,"pageSize":2,"sort":"relevance"}',
            max_records=2, max_pages=1,
        )
        page_url = (
            "https://www.reddit.com/search.rss?q=best+office+chair+back+pain"
            "&type=link&limit=2"
        )
        for fixture in (
            "search-invalid-entry.rss", "search-truncated.rss", "search-no-feed.rss",
        ):
            with self.subTest(fixture=fixture):
                capture = capture_reddit_pages(
                    request,
                    (RedditPage(url=page_url, html=(FIXTURES / fixture).read_text(encoding="utf-8")),),
                    captured_at=NOW,
                )
                self.assertFalse(capture.result.envelope.complete)
                self.assertEqual(capture.result.records, ())
                self.assertEqual(capture.result.envelope.gaps[0].code, "selector-drift")

    def test_invalid_second_search_page_preserves_records_but_is_not_complete(self):
        request = SourceRequest(
            source="reddit", operation="search", locator="best office chair back pain",
            parameters_json='{"maxComments":100,"pageSize":2,"sort":"relevance"}',
            max_records=4, max_pages=2,
        )
        first_url = (
            "https://www.reddit.com/search.rss?q=best+office+chair+back+pain"
            "&type=link&limit=2"
        )
        second_url = first_url + "&after=t3_1x0exqu&count=2"
        for fixture in (
            "search-invalid-entry.rss", "search-truncated.rss", "search-no-feed.rss",
        ):
            with self.subTest(fixture=fixture):
                capture = capture_reddit_pages(
                    request,
                    (
                        RedditPage(url=first_url, html=(FIXTURES / "search.rss").read_text(encoding="utf-8")),
                        RedditPage(url=second_url, html=(FIXTURES / fixture).read_text(encoding="utf-8")),
                    ),
                    captured_at=NOW,
                )
                self.assertFalse(capture.result.envelope.complete)
                self.assertEqual(len(capture.result.records), 2)
                self.assertEqual(capture.result.envelope.gaps[0].code, "selector-drift")

    def test_exhausted_feed_rate_limit_preserves_prior_page_evidence(self):
        request = SourceRequest(
            source="reddit", operation="search", locator="best office chair back pain",
            parameters_json='{"maxComments":100,"pageSize":2,"sort":"relevance"}',
            max_records=4, max_pages=2,
        )
        first_url = (
            "https://www.reddit.com/search.rss?q=best+office+chair+back+pain"
            "&type=link&limit=2"
        )
        second_url = first_url + "&after=t3_1x0exqu&count=2"
        responses = [
            (first_url, (FIXTURES / "search.rss").read_text(encoding="utf-8"), 200),
            (second_url, "rate limited", 429),
            (second_url, "rate limited", 429),
            (second_url, "rate limited", 429),
        ]

        class Page:
            def __init__(self):
                self.url = "about:blank"
                self.html = ""

            def route(self, *_args):
                return None

            def goto(self, url, **_kwargs):
                expected, self.html, status = responses.pop(0)
                self.url = url
                if url != expected:
                    raise AssertionError(f"unexpected page {url}")
                return SimpleNamespace(status=status)

            def locator(self, _selector):
                return SimpleNamespace(inner_text=lambda: self.html)

        page = Page()
        context = SimpleNamespace(new_page=lambda: page, close=lambda: None)
        class PlaywrightContext:
            def __enter__(self):
                return SimpleNamespace()

            def __exit__(self, *_args):
                return None

        playwright_context = PlaywrightContext()
        with patch("patchright.sync_api.sync_playwright", return_value=playwright_context), patch.object(
            scraper_module, "_open_browser_context", return_value=(context, None),
        ), patch.object(scraper_module.time, "sleep", return_value=None):
            capture = scraper_module.acquire_reddit(
                request, budgets=SourceBudgets(), settings_digest="a" * 64,
                profile_root=None, native_transport=True,
            )
        self.assertEqual(len(capture.result.records), 2)
        self.assertFalse(capture.result.envelope.complete)
        self.assertEqual(capture.result.envelope.gaps[0].code, "rate-limited")

    def test_feed_honors_retry_after_and_retries_the_same_cursor(self):
        request = SourceRequest(
            source="reddit", operation="search", locator="best office chair back pain",
            parameters_json='{"maxComments":100,"pageSize":2,"sort":"relevance"}',
            max_records=4, max_pages=2,
        )
        first_url = (
            "https://www.reddit.com/search.rss?q=best+office+chair+back+pain"
            "&type=link&limit=2"
        )
        second_url = first_url + "&after=t3_1x0exqu&count=2"
        responses = [
            (first_url, (FIXTURES / "search.rss").read_text(encoding="utf-8"), 200, None),
            (second_url, "rate limited", 429, "2"),
            (second_url, (FIXTURES / "external-link.rss").read_text(encoding="utf-8"), 200, None),
        ]

        class Response:
            def __init__(self, status, retry_after):
                self.status = status
                self._retry_after = retry_after

            def header_value(self, name):
                return self._retry_after if name.casefold() == "retry-after" else None

        class Page:
            def __init__(self):
                self.url = "about:blank"
                self.html = ""

            def route(self, *_args):
                return None

            def goto(self, url, **_kwargs):
                expected, self.html, status, retry_after = responses.pop(0)
                self.url = url
                if url != expected:
                    raise AssertionError(f"unexpected page {url}")
                return Response(status, retry_after)

            def locator(self, _selector):
                return SimpleNamespace(inner_text=lambda: self.html)

        page = Page()
        context = SimpleNamespace(new_page=lambda: page, close=lambda: None)
        class PlaywrightContext:
            def __enter__(self): return SimpleNamespace()
            def __exit__(self, *_args): return None

        waits = []
        with patch("patchright.sync_api.sync_playwright", return_value=PlaywrightContext()), patch.object(
            scraper_module, "_open_browser_context", return_value=(context, None),
        ), patch.object(scraper_module, "_interruptible_wait",
                        side_effect=lambda delay, **_kwargs: waits.append(delay)):
            capture = scraper_module.acquire_reddit(
                request, budgets=SourceBudgets(timeout_seconds=30), settings_digest="a" * 64,
                profile_root=None, native_transport=True,
            )

        self.assertEqual(len(capture.result.records), 3)
        self.assertTrue(capture.result.envelope.complete)
        self.assertIn(2.0, waits)
        self.assertEqual(responses, [])

    def test_live_invalid_second_feed_preserves_the_first_page(self):
        request = SourceRequest(
            source="reddit", operation="search", locator="best office chair back pain",
            parameters_json='{"maxComments":100,"pageSize":2,"sort":"relevance"}',
            max_records=4, max_pages=2,
        )
        first_url = (
            "https://www.reddit.com/search.rss?q=best+office+chair+back+pain"
            "&type=link&limit=2"
        )
        second_url = first_url + "&after=t3_1x0exqu&count=2"
        responses = [
            (first_url, (FIXTURES / "search.rss").read_text(encoding="utf-8"), 200),
            (second_url, (FIXTURES / "search-invalid-entry.rss").read_text(encoding="utf-8"), 200),
        ]

        class Page:
            def __init__(self):
                self.url = "about:blank"
                self.html = ""

            def route(self, *_args): return None

            def goto(self, url, **_kwargs):
                expected, self.html, status = responses.pop(0)
                self.url = url
                if url != expected:
                    raise AssertionError(f"unexpected page {url}")
                return SimpleNamespace(status=status)

            def locator(self, _selector):
                return SimpleNamespace(inner_text=lambda: self.html)

        page = Page()
        context = SimpleNamespace(new_page=lambda: page, close=lambda: None)
        class PlaywrightContext:
            def __enter__(self): return SimpleNamespace()
            def __exit__(self, *_args): return None

        with patch("patchright.sync_api.sync_playwright", return_value=PlaywrightContext()), patch.object(
            scraper_module, "_open_browser_context", return_value=(context, None),
        ), patch.object(scraper_module.time, "sleep", return_value=None):
            capture = scraper_module.acquire_reddit(
                request, budgets=SourceBudgets(), settings_digest="a" * 64,
                profile_root=None, native_transport=True,
            )

        self.assertEqual(len(capture.result.records), 2)
        self.assertFalse(capture.result.envelope.complete)
        self.assertEqual(capture.result.envelope.gaps[0].code, "selector-drift")

    def test_bounded_partial_research_capture_is_reused_from_cache(self):
        captured_at = datetime.now(timezone.utc)
        request = SourceRequest(
            source="reddit", operation="search", locator="best office chair back pain",
            parameters_json='{"maxComments":100,"pageSize":2,"sort":"relevance"}',
            max_records=1, max_pages=1,
        )
        page_url = (
            "https://www.reddit.com/search.rss?q=best+office+chair+back+pain"
            "&type=link&limit=2"
        )
        capture = capture_reddit_pages(
            request,
            (RedditPage(url=page_url, html=(FIXTURES / "search.rss").read_text(encoding="utf-8")),),
            captured_at=captured_at,
        ).model_copy(update={"settings_digest": "a" * 64})
        self.assertFalse(capture.result.envelope.complete)
        self.assertEqual(capture.result.envelope.gaps[0].code, "record-limit")

        with tempfile.TemporaryDirectory(dir=Path.home()) as directory:
            cache_fd = open_private_cache(Path(directory) / "cache")
            writer_fd = os.dup(cache_fd) if isinstance(cache_fd, int) else cache_fd
            try:
                cache = scraper_module.RedditCache.from_descriptor(writer_fd)
                key = scraper_module._canonical_json({
                    "sourceSliceDigest": scraper_module.source_slice_digest(request),
                    "settingsDigest": "a" * 64,
                })
                cache.put(
                    key, json.loads(capture.model_dump_json()),
                    captured_at=captured_at.timestamp(),
                )
                cached = scraper_module.acquire_reddit(
                    request, budgets=SourceBudgets(), settings_digest="a" * 64,
                    cache_fd=cache_fd, profile_root=Path("/unreachable/profile"),
                    native_transport=True,
                )
            finally:
                if isinstance(writer_fd, int):
                    os.close(writer_fd)
                if isinstance(cache_fd, int):
                    os.close(cache_fd)
        self.assertEqual(cached.acquisition_mode, "cache-hit")
        self.assertEqual(cached.result.records, capture.result.records)

    def test_thread_feed_returns_post_and_visible_comment_text(self):
        request = SourceRequest(
            source="reddit", operation="thread", locator="16fqhts",
            parameters_json='{"maxComments":1,"pageSize":2}',
            max_records=2, max_pages=1,
        )
        page_url = "https://www.reddit.com/comments/16fqhts/.rss?limit=2"
        capture = capture_reddit_pages(
            request,
            (RedditPage(url=page_url, html=(FIXTURES / "thread.rss").read_text(encoding="utf-8")),),
            captured_at=NOW,
        )
        rows = [(record.record_type, json.loads(record.data_json))
                for record in capture.result.records]
        self.assertEqual([kind for kind, _row in rows], ["thread", "comment"])
        self.assertIn("swelling, redness and pain", rows[0][1]["body"])
        self.assertEqual(rows[1][1]["body"], "Hot and throbbing...")
        self.assertEqual(rows[1][1]["author"], "Seigneur_aide_moi")
        legacy = cli_module._legacy(capture, "thread")
        self.assertIsNone(legacy["post"]["score"])
        self.assertIsNone(legacy["comments"][0]["score"])
        self.assertEqual(legacy["post"]["permalink"], "https://www.reddit.com/comments/16fqhts/")
        self.assertEqual(legacy["comments"][0]["id"], "k07805h")
        self.assertEqual(
            legacy["comments"][0]["permalink"],
            "https://www.reddit.com/r/bunions/comments/16fqhts/what_is_bunion_pain_like/k07805h/",
        )
        self.assertEqual(legacy["comments"][0]["age"], "2023-09-12T02:03:08+00:00")
        self.assertFalse(capture.result.envelope.complete)
        self.assertEqual(capture.result.envelope.gaps[0].code, "comment-limit")

    def test_thread_feed_without_requested_post_fails_closed(self):
        request = SourceRequest(
            source="reddit", operation="thread", locator="16fqhts",
            parameters_json='{"maxComments":10,"pageSize":11}',
            max_records=11, max_pages=1,
        )
        capture = capture_reddit_pages(
            request,
            (RedditPage(
                url="https://www.reddit.com/comments/16fqhts/.rss?limit=11",
                html=(FIXTURES / "thread-comments-only.rss").read_text(encoding="utf-8"),
            ),),
            captured_at=NOW,
        )
        self.assertFalse(capture.result.envelope.complete)
        self.assertEqual(capture.result.records, ())
        self.assertEqual(capture.result.envelope.gaps[0].code, "selector-drift")

    def test_about_accepts_reddits_canonical_subreddit_casing(self):
        try:
            _validate_page_url(
                "about", "python", "https://www.reddit.com/r/python/",
                "https://www.reddit.com/r/Python/",
            )
        except Exception as error:
            self.fail(f"canonical subreddit casing was rejected: {error}")

    def test_about_capture_accepts_reddits_canonical_subreddit_casing(self):
        request = SourceRequest(
            source="reddit", operation="about", locator="python",
            parameters_json='{"maxComments":100,"pageSize":1}',
            max_records=1, max_pages=1,
        )
        capture = capture_reddit_pages(
            request,
            (RedditPage(
                url="https://www.reddit.com/r/Python/",
                html=(
                    '<shreddit-subreddit-header prefixed-name="r/Python" '
                    'display-name="Python" weekly-active-users="10" '
                    'weekly-contributions="2" description="Community">'
                    '</shreddit-subreddit-header>'
                ),
            ),),
            captured_at=NOW,
        )
        self.assertTrue(capture.result.envelope.complete)

    def test_about_missing_activity_metrics_remain_unknown(self):
        request = SourceRequest(
            source="reddit", operation="about", locator="python",
            parameters_json='{"maxComments":100,"pageSize":1}',
            max_records=1, max_pages=1,
        )
        capture = capture_reddit_pages(
            request,
            (RedditPage(
                url="https://www.reddit.com/r/python/",
                html='<shreddit-subreddit-header prefixed-name="r/python" />',
            ),),
            captured_at=NOW,
        )
        data = cli_module._legacy(capture, "about")
        self.assertIsNone(data["weekly_active_users"])
        self.assertIsNone(data["weekly_contributions"])

    def test_about_human_output_marks_missing_activity_unknown(self):
        request = SourceRequest(
            source="reddit", operation="about", locator="python",
            parameters_json='{"maxComments":100,"pageSize":1}',
            max_records=1, max_pages=1,
        )
        capture = capture_reddit_pages(
            request,
            (RedditPage(
                url="https://www.reddit.com/r/python/",
                html='<shreddit-subreddit-header prefixed-name="r/python" />',
            ),),
            captured_at=NOW,
        )
        stdout = StringIO()
        with tempfile.TemporaryDirectory(dir=Path.home()) as directory:
            with redirect_stdout(stdout):
                code = main(
                    ["about", "python", "--fresh"],
                    acquire=lambda *_args, **_kwargs: capture,
                    cache_root=Path(directory) / "cache",
                    profile_root=Path(directory) / "profile",
                )
        self.assertEqual(code, 0)
        self.assertIn("? weekly active users", stdout.getvalue())
        self.assertIn("? weekly posts+comments", stdout.getvalue())

    def test_redirect_error_reports_the_landed_on_url(self):
        landed_on = (
            "https://old.reddit.com/login/?reason=lor2&"
            "dest=https%3A%2F%2Fold.reddit.com%2Fsearch%3Fq%3Dbunion%2Bpain"
        )
        with self.assertRaises(RedditCaptureError) as caught:
            _validate_page_url(
                "search", "bunion pain",
                "https://old.reddit.com/search?q=bunion+pain", landed_on,
            )
        self.assertEqual(caught.exception.code, "redirect-denied")
        self.assertIn(f"landed on {landed_on}", str(caught.exception))

    def test_cross_origin_redirect_error_reports_the_landed_on_url(self):
        landed_on = "https://www.reddit.com/login/?reason=lor2"
        with self.assertRaises(RedditCaptureError) as caught:
            _validate_page_url(
                "search", "bunion pain",
                "https://old.reddit.com/search?q=bunion+pain", landed_on,
            )
        self.assertEqual(caught.exception.code, "redirect-denied")
        self.assertIn(f"landed on {landed_on}", str(caught.exception))

    def test_malformed_redirect_is_typed_and_reports_the_landed_on_url(self):
        landed_on = "https://old.reddit.com:invalid/search?q=bunion+pain"
        with self.assertRaises(RedditCaptureError) as caught:
            _validate_page_url(
                "search", "bunion pain",
                "https://old.reddit.com/search?q=bunion+pain", landed_on,
            )
        self.assertEqual(caught.exception.code, "redirect-denied")
        self.assertIn(f"landed on {landed_on}", str(caught.exception))

    def test_unapproved_search_parameter_reports_the_landed_on_url(self):
        landed_on = "https://old.reddit.com/search?q=bunion+pain&after=not-a-post"
        with self.assertRaises(RedditCaptureError) as caught:
            _validate_page_url(
                "search", "bunion pain",
                "https://old.reddit.com/search?q=bunion+pain", landed_on,
            )
        self.assertEqual(caught.exception.code, "redirect-denied")
        self.assertIn(f"landed on {landed_on}", str(caught.exception))

    def test_browser_boundary_is_get_only_and_reddit_only(self):
        self.assertTrue(allow_browser_request(
            "GET", "https://old.reddit.com/r/python/",
            request_count=1, max_requests=10,
        ))
        self.assertFalse(allow_browser_request(
            "POST", "https://old.reddit.com/r/python/",
            request_count=1, max_requests=10,
        ))
        self.assertFalse(allow_browser_request(
            "GET", "https://example.com/r/python/",
            request_count=1, max_requests=10,
        ))
        self.assertFalse(allow_browser_request(
            "GET", "https://www.redditstatic.com/js/concat",
            request_count=1, max_requests=10,
        ))
        self.assertFalse(allow_browser_request(
            "GET", "https://old.reddit.com/api/vote",
            request_count=1, max_requests=10,
        ))
        self.assertFalse(allow_browser_request(
            "GET", "https://www.reddit.com/r/python.json",
            request_count=1, max_requests=10,
        ))
        self.assertFalse(allow_browser_request(
            "GET", "https://www.reddit.com/account/login",
            request_count=1, max_requests=10,
        ))

    def test_browser_subresources_are_static_assets_only(self):
        policy = getattr(scraper_module, "allow_browser_subresource", None)
        self.assertIsNotNone(policy)
        if policy is None:
            return
        self.assertTrue(policy("GET", "https://www.redditstatic.com/js/concat"))
        self.assertFalse(policy("POST", "https://www.redditstatic.com/js/concat"))
        self.assertFalse(policy("GET", "https://www.reddit.com/r/python.json"))
        self.assertFalse(policy("GET", "https://www.reddit.com/message/inbox"))

    def test_persistent_acquisition_launches_the_named_profile(self):
        opener = getattr(scraper_module, "_open_browser_context", None)
        self.assertIsNotNone(opener)
        if opener is None:
            return

        class Chromium:
            def __init__(self):
                self.persistent = None
                self.ephemeral = False

            def launch_persistent_context(self, path, **kwargs):
                self.persistent = (path, kwargs)
                return "persistent-context"

            def launch(self, **_kwargs):
                self.ephemeral = True
                return SimpleNamespace(new_context=lambda **_options: "ephemeral-context")

        chromium = Chromium()
        context, browser = opener(
            SimpleNamespace(chromium=chromium),
            profile_root=Path("/tmp/reddita-profile"),
            allow_stealth=True,
        )
        self.assertEqual(context, "persistent-context")
        self.assertIsNone(browser)
        self.assertFalse(chromium.ephemeral)
        path, options = chromium.persistent
        self.assertEqual(path, str(Path("/tmp/reddita-profile")))
        self.assertIn("--disable-blink-features=AutomationControlled", options["args"])

    def test_browser_user_agent_matches_platform_and_chromium_major(self):
        class Chromium:
            executable_path = "/fake/chrome"

            def launch_persistent_context(self, _path, **kwargs):
                self.options = kwargs
                return "persistent-context"

        chromium = Chromium()
        version = SimpleNamespace(stdout="Google Chrome for Testing 156.0.8078.4\n")
        with patch.object(sys, "platform", "darwin"), patch.object(
            subprocess, "run", return_value=version,
        ):
            scraper_module._open_browser_context(
                SimpleNamespace(chromium=chromium),
                profile_root=Path("/private/reddita-profile"),
                allow_stealth=True,
            )
        user_agent = chromium.options["user_agent"]
        self.assertIn("Macintosh; Intel Mac OS X 10_15_7", user_agent)
        self.assertIn("Chrome/156.0.0.0", user_agent)

    def test_failed_version_probe_keeps_chromiums_native_user_agent(self):
        class Chromium:
            executable_path = "/missing/chrome"

            def launch_persistent_context(self, _path, **kwargs):
                self.options = kwargs
                return "persistent-context"

        chromium = Chromium()
        with patch.object(subprocess, "run", side_effect=OSError("missing")):
            scraper_module._open_browser_context(
                SimpleNamespace(chromium=chromium),
                profile_root=Path("/private/reddita-profile"),
                allow_stealth=True,
            )
        self.assertNotIn("user_agent", chromium.options)

    def test_system_chrome_channel_keeps_its_native_user_agent(self):
        class Chromium:
            executable_path = "/bundled/chromium"

            def launch_persistent_context(self, _path, **kwargs):
                self.options = kwargs
                return "persistent-context"

        chromium = Chromium()
        scraper_module._open_browser_context(
            SimpleNamespace(chromium=chromium),
            profile_root=Path("/private/reddita-profile"),
            allow_stealth=True,
            channel="chrome",
        )
        self.assertEqual(chromium.options["channel"], "chrome")
        self.assertNotIn("user_agent", chromium.options)

    def test_persistent_browser_allows_get_and_aborts_state_changing_methods(self):
        policy = getattr(scraper_module, "_readonly_browser_route", None)
        self.assertIsNotNone(policy)
        if policy is None:
            return

        class Route:
            def __init__(self, method):
                self.request = SimpleNamespace(method=method)
                self.action = None

            def continue_(self):
                self.action = "continue"

            def abort(self):
                self.action = "abort"

        get_route, post_route = Route("GET"), Route("POST")
        policy(get_route)
        policy(post_route)
        self.assertEqual(get_route.action, "continue")
        self.assertEqual(post_route.action, "abort")

    def test_transport_rejects_mixed_public_and_private_dns(self):
        boundary = PublicHttpsBoundary(
            allowed_hosts={"old.reddit.com"},
            resolver=lambda *_args, **_kwargs: [
                (2, 1, 6, "", ("93.184.216.34", 443)),
                (2, 1, 6, "", ("10.0.0.2", 443)),
            ],
        )
        with self.assertRaises(PublicTransportError) as caught:
            boundary.resolve("https://old.reddit.com/r/python/")
        self.assertEqual(caught.exception.code, "mixed-addresses-denied")

    def test_listing_parser_returns_stable_json_record(self):
        request = SourceRequest(
            source="reddit", operation="subreddit", locator="BuyItForLife",
            parameters_json='{"maxComments":100,"pageSize":25,"sort":"top"}',
            max_records=1, max_pages=1,
        )
        capture = capture_reddit_pages(
            request,
            (RedditPage(
                url="https://www.reddit.com/r/buyitforlife/top/.rss?t=all&limit=25",
                html=(FIXTURES / "subreddit.rss").read_text(encoding="utf-8"),
            ),),
            captured_at=NOW,
        )
        self.assertTrue(capture.result.envelope.complete)
        self.assertEqual(capture.result.envelope.record_count, 1)
        row = json.loads(capture.result.records[0].data_json)
        self.assertEqual(
            (row["id"], row["author"], row["subreddit"], row["score"]),
            ("ab7l6x", "readwritethink", "BuyItForLife", None),
        )
        self.assertIn("stopped its lifetime updates", row["body"])

    def test_cli_json_is_agent_friendly_and_uses_private_cache(self):
        request = SourceRequest(
            source="reddit", operation="search", locator="office chair",
            parameters_json='{"maxComments":100,"pageSize":25,"sort":"relevance"}',
            max_records=1, max_pages=1,
        )
        capture = capture_reddit_pages(
            request,
            (RedditPage(
                url=("https://www.reddit.com/search.rss?q=office+chair"
                     "&type=link&limit=25"),
                html=(FIXTURES / "cli-search.rss").read_text(encoding="utf-8"),
            ),),
            captured_at=NOW,
        )
        observed = {}

        def acquire(source_request, **kwargs):
            observed["request"] = source_request
            observed["cache"] = kwargs["cache_fd"]
            observed["profile"] = kwargs.get("profile_root")
            observed["native_transport"] = kwargs.get("native_transport")
            return capture

        stdout, stderr = StringIO(), StringIO()
        with tempfile.TemporaryDirectory(dir=Path.home()) as directory:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(
                    ["search", "office chair", "60", "--json"],
                    acquire=acquire,
                    cache_root=Path(directory) / "cache",
                    profile_root=Path(directory) / "profile",
                )
        self.assertEqual((code, stderr.getvalue()), (0, ""))
        output = json.loads(stdout.getvalue())[0]
        self.assertIn("comfortable chair", output["title"])
        self.assertIn("10–12 hours", output["body"])
        self.assertIsNotNone(observed["cache"])
        self.assertEqual(observed["request"].max_records, 60)
        self.assertEqual(observed["request"].max_pages, 3)
        self.assertEqual(observed["profile"], Path(directory) / "profile")
        self.assertTrue(observed["native_transport"])

    def test_cli_extended_mode_builds_a_bounded_2500_post_capture(self):
        request = SourceRequest(
            source="reddit", operation="search", locator="office chair",
            parameters_json='{"maxComments":100,"pageSize":25,"sort":"relevance"}',
            max_records=1, max_pages=1,
        )
        capture = capture_reddit_pages(
            request,
            (RedditPage(
                url=("https://www.reddit.com/search.rss?q=office+chair&type=link&limit=25"),
                html=(FIXTURES / "cli-search.rss").read_text(encoding="utf-8"),
            ),),
            captured_at=NOW,
        )
        observed = {}

        def acquire(source_request, **kwargs):
            observed["request"] = source_request
            observed["budgets"] = kwargs["budgets"]
            return capture

        with tempfile.TemporaryDirectory(dir=Path.home()) as directory:
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                code = main(
                    ["search", "office chair", "2500", "--extended", "--json"],
                    acquire=acquire, cache_root=Path(directory) / "cache",
                    profile_root=Path(directory) / "profile",
                )

        self.assertEqual(code, 0)
        self.assertEqual(observed["request"].max_records, 2500)
        self.assertEqual(observed["request"].max_pages, 100)
        self.assertEqual(observed["budgets"].timeout_seconds, 3600)
        self.assertEqual(observed["budgets"].max_requests, 1000)

    def test_cli_json_exposes_partial_capture_metadata(self):
        request = SourceRequest(
            source="reddit", operation="search", locator="best office chair back pain",
            parameters_json='{"maxComments":100,"pageSize":2,"sort":"relevance"}',
            max_records=1, max_pages=1,
        )
        capture = capture_reddit_pages(
            request,
            (RedditPage(
                url=("https://www.reddit.com/search.rss?q=best+office+chair+back+pain"
                     "&type=link&limit=2"),
                html=(FIXTURES / "search.rss").read_text(encoding="utf-8"),
            ),),
            captured_at=NOW,
        )
        stdout, stderr = StringIO(), StringIO()
        with tempfile.TemporaryDirectory(dir=Path.home()) as directory:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(
                    ["search", "best office chair back pain", "1", "--json-meta", "--fresh"],
                    acquire=lambda *_args, **_kwargs: capture,
                    cache_root=Path(directory) / "cache",
                    profile_root=Path(directory) / "profile",
                )
        document = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertFalse(document["complete"])
        self.assertEqual(document["gaps"][0]["code"], "record-limit")
        self.assertEqual(len(document["results"]), 1)
        self.assertIn("partial", stderr.getvalue())

    def test_help_is_a_successful_stdout_command(self):
        stdout = StringIO()
        with redirect_stdout(stdout):
            code = main(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("Usage:", stdout.getvalue())

    def test_human_output_marks_unavailable_feed_metrics_as_unknown(self):
        stdout = StringIO()
        with redirect_stdout(stdout):
            cli_module._print_posts([{
                "score": None, "comments": None, "subreddit": "OfficeChairs",
                "title": "Chair research", "permalink": "https://www.reddit.com/comments/abc123/",
            }])
        self.assertIn("?▲", stdout.getvalue())
        self.assertIn("?💬", stdout.getvalue())

    def test_cli_rejects_unknown_options_and_invalid_argument_shapes(self):
        cases = (
            ["search", "chairs", "many"],
            ["search", "chairs", "0"],
            ["search", "chairs", "101"],
            ["search", "chairs", "5", "--bogus"],
            ["about", "python", "extra"],
            ["thread", "16fqhts", "5", "--top"],
            ["thread", "16fqhts", "100"],
            ["sub", "python", "5", "--new", "--top"],
        )
        for arguments in cases:
            with self.subTest(arguments=arguments), self.assertRaises(SystemExit):
                main(arguments, acquire=lambda *_args, **_kwargs: self.fail("acquired"))

    def test_cli_rejects_an_incomplete_capture_instead_of_printing_empty_success(self):
        incomplete = SimpleNamespace(result=SourceResult(
            envelope=CaptureEnvelope(
                schema_version="1", source="reddit", operation="about",
                requested_locator="python", final_locator="https://www.reddit.com/r/python/",
                captured_at=NOW, collection_mode="rendered", complete=False,
                record_count=0, continuation=None,
                gaps=(CaptureGap(
                    code="empty-results", detail="Reddit returned no observable records",
                    locator="https://www.reddit.com/r/python/",
                ),),
            ),
            records=(), pages=(),
        ))
        with tempfile.TemporaryDirectory(dir=Path.home()) as directory:
            with self.assertRaises(SystemExit) as caught:
                main(
                    ["about", "python", "--json", "--fresh"],
                    acquire=lambda *_args, **_kwargs: incomplete,
                    cache_root=Path(directory) / "cache",
                    profile_root=Path(directory) / "profile",
                )
        self.assertIn("empty-results", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
