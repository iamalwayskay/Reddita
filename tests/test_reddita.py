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
from reddita.contract import RedditContractError, normalize_reddit_request
from reddita.models import CaptureEnvelope, CaptureGap
from reddita.scraper import RedditCaptureError, RedditPage, capture_reddit_pages
from reddita.scraper import allow_browser_request
from reddita.scraper import _validate_page_url
import reddita.scraper as scraper_module
from reddita.transport import PublicHttpsBoundary, PublicTransportError


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
FIXTURES = Path(__file__).parent / "fixtures"


class RedditaTests(unittest.TestCase):
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
            (RedditPage(url=page_url, html=(FIXTURES / "search.rss").read_text()),),
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
                    (RedditPage(url=page_url, html=(FIXTURES / fixture).read_text()),),
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
                        RedditPage(url=first_url, html=(FIXTURES / "search.rss").read_text()),
                        RedditPage(url=second_url, html=(FIXTURES / fixture).read_text()),
                    ),
                    captured_at=NOW,
                )
                self.assertFalse(capture.result.envelope.complete)
                self.assertEqual(len(capture.result.records), 2)
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
            (RedditPage(url=page_url, html=(FIXTURES / "search.rss").read_text()),),
            captured_at=captured_at,
        ).model_copy(update={"settings_digest": "a" * 64})
        self.assertFalse(capture.result.envelope.complete)
        self.assertEqual(capture.result.envelope.gaps[0].code, "record-limit")

        with tempfile.TemporaryDirectory(dir=Path.home()) as directory:
            cache_fd = open_private_cache(Path(directory) / "cache")
            writer_fd = os.dup(cache_fd)
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
                os.close(writer_fd)
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
            (RedditPage(url=page_url, html=(FIXTURES / "thread.rss").read_text()),),
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
                html=(FIXTURES / "thread-comments-only.rss").read_text(),
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
        self.assertEqual(path, "/tmp/reddita-profile")
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
                html=(FIXTURES / "subreddit.rss").read_text(),
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
                html=(FIXTURES / "cli-search.rss").read_text(),
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
