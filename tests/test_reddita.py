from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from reddita import SourceRequest, SourceResult
from reddita.cli import main
from reddita.contract import RedditContractError, normalize_reddit_request
from reddita.models import CaptureEnvelope, CaptureGap
from reddita.scraper import RedditCaptureError, RedditPage, capture_reddit_pages
from reddita.scraper import allow_browser_request
from reddita.scraper import _validate_page_url
import reddita.scraper as scraper_module
from reddita.transport import PublicHttpsBoundary, PublicTransportError


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class RedditaTests(unittest.TestCase):
    def test_search_contract_is_canonical_and_bounded(self):
        identity, url = normalize_reddit_request(
            "search", "python tools", {"pageSize": 10, "maxComments": 100},
        )
        self.assertEqual(identity, "python tools")
        self.assertEqual(url, "https://old.reddit.com/search?q=python+tools")
        with self.assertRaises(RedditContractError):
            normalize_reddit_request(
                "search", "python", {"pageSize": 101, "maxComments": 100},
            )

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
            "GET", "https://old.reddit.com/api/vote",
            request_count=1, max_requests=10,
        ))

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
            source="reddit", operation="search", locator="python tools",
            parameters_json='{"maxComments":100,"pageSize":1}',
            max_records=1, max_pages=1,
        )
        capture = capture_reddit_pages(
            request,
            (RedditPage(
                url="https://old.reddit.com/search?q=python+tools",
                html=(
                    '<div class="thing" data-fullname="t3_abc123" '
                    'data-subreddit="python" data-author="alice" data-score="42">'
                    '<a class="title" href="https://example.test/article">First post</a>'
                    '<a class="comments" href="/r/python/comments/abc123/post/">12 comments</a>'
                    '</div>'
                ),
            ),),
            captured_at=NOW,
        )
        self.assertTrue(capture.result.envelope.complete)
        self.assertEqual(capture.result.envelope.record_count, 1)
        row = json.loads(capture.result.records[0].data_json)
        self.assertEqual(
            (row["id"], row["title"], row["score"], row["commentCount"]),
            ("abc123", "First post", 42, 12),
        )

    def test_cli_json_is_agent_friendly_and_uses_private_cache(self):
        request = SourceRequest(
            source="reddit", operation="search", locator="fixture",
            parameters_json='{"maxComments":100,"pageSize":1}',
            max_records=1, max_pages=1,
        )
        capture = capture_reddit_pages(
            request,
            (RedditPage(
                url="https://old.reddit.com/search?q=fixture",
                html=(
                    '<div class="thing" data-fullname="t3_abc123" '
                    'data-subreddit="python" data-score="1">'
                    '<a class="title" href="https://example.test">Fixture</a>'
                    '<a class="comments" href="/r/python/comments/abc123/post/">0 comments</a>'
                    '</div>'
                ),
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
                    ["search", "fixture", "1", "--json"],
                    acquire=acquire,
                    cache_root=Path(directory) / "cache",
                    profile_root=Path(directory) / "profile",
                )
        self.assertEqual((code, stderr.getvalue()), (0, ""))
        self.assertEqual(json.loads(stdout.getvalue())[0]["title"], "Fixture")
        self.assertIsNotNone(observed["cache"])
        self.assertEqual(observed["request"].max_records, 1)
        self.assertEqual(observed["profile"], Path(directory) / "profile")
        self.assertTrue(observed["native_transport"])

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
