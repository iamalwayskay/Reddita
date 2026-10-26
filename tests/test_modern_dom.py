from __future__ import annotations

from pathlib import Path
import unittest

from reddita.modern_dom import (
    ModernDomError, parse_about_dom, parse_listing_dom, parse_thread_dom,
)


FIXTURES = Path(__file__).parent / "fixtures"


class ModernRedditDomParserTests(unittest.TestCase):
    def test_search_telemetry_posts_preserve_text_metrics_links_and_next_cursor(self):
        rows, after = parse_listing_dom(
            (FIXTURES / "modern-search.html").read_text(encoding="utf-8"),
            "https://www.reddit.com/search/?q=repairable+blender",
        )

        self.assertEqual(after, "t3_web002")
        self.assertEqual([row[:2] for row in rows], [
            ("web001", "post"), ("web002", "post"),
        ])
        first, second = rows[0][2], rows[1][2]
        self.assertEqual((first["score"], first["commentCount"]), (204, 37))
        self.assertEqual(first["body"], "The drive socket is replaceable.")
        self.assertIsNone(first["externalUrl"])
        self.assertEqual(second["externalUrl"], "https://example.com/blenders/specification")
        self.assertEqual(first["surface"], "web")

    def test_live_search_sdui_card_preserves_tracking_identity_metrics_and_text(self):
        rows, after = parse_listing_dom(
            (FIXTURES / "modern-search-sdui.html").read_text(encoding="utf-8"),
            "https://www.reddit.com/search/?q=repairable+vacuum",
        )

        self.assertIsNone(after)
        self.assertEqual(len(rows), 1)
        identifier, record_type, post = rows[0]
        self.assertEqual((identifier, record_type), ("live01", "post"))
        self.assertEqual((post["author"], post["subreddit"]), ("repair_researcher", "BuyItForLife"))
        self.assertEqual((post["score"], post["commentCount"]), (6, 22))
        self.assertEqual(post["body"], "The motor brushes and hose are sold separately.")
        self.assertEqual(post["age"], "2025-03-04T05:06:07.000Z")
        self.assertEqual(
            post["permalink"],
            "https://www.reddit.com/r/BuyItForLife/comments/live01/a_vacuum_with_replaceable_parts/",
        )

    def test_shreddit_thread_preserves_recursive_comment_relationships(self):
        rows, truncated = parse_thread_dom(
            (FIXTURES / "modern-thread.html").read_text(encoding="utf-8"),
            "https://www.reddit.com/r/BuyItForLife/comments/thrweb/repairable_coffee_grinder/",
            max_comments=2,
        )

        self.assertFalse(truncated)
        self.assertEqual([row[:2] for row in rows], [
            ("thrweb", "thread"), ("com001", "comment"), ("com002", "comment"),
        ])
        post, top, reply = rows[0][2], rows[1][2], rows[2][2]
        self.assertEqual((post["score"], post["commentCount"]), (91, 2))
        self.assertEqual(post["body"], "I want replaceable burrs and a service manual.")
        self.assertEqual((top["parentId"], top["depth"], top["topLevel"]), ("thrweb", 0, True))
        self.assertEqual((reply["parentId"], reply["depth"], reply["topLevel"]), ("com001", 1, False))
        self.assertEqual(reply["permalink"], "https://www.reddit.com/r/BuyItForLife/comments/thrweb/repairable_coffee_grinder/com001/com002/")

    def test_shreddit_thread_infers_depth_and_marks_unrendered_comments_truncated(self):
        rows, truncated = parse_thread_dom(
            (FIXTURES / "modern-thread-inferred-partial.html").read_text(encoding="utf-8"),
            "https://www.reddit.com/r/BuyItForLife/comments/partial1/partial_thread/",
            max_comments=20,
        )

        self.assertTrue(truncated)
        self.assertEqual((rows[1][2]["depth"], rows[2][2]["depth"]), (0, 1))
        self.assertEqual((rows[1][2]["parentId"], rows[2][2]["parentId"]), ("partial1", "parent1"))

    def test_shreddit_thread_rejects_post_permalink_for_another_identifier(self):
        with self.assertRaises(ModernDomError):
            parse_thread_dom(
                (FIXTURES / "modern-thread-mismatched-permalink.html").read_text(encoding="utf-8"),
                "https://www.reddit.com/r/BuyItForLife/comments/right1/mismatched_thread/",
                max_comments=20,
            )

    def test_about_dom_reads_standard_subreddit_header(self):
        rows = parse_about_dom(
            (FIXTURES / "modern-about.html").read_text(encoding="utf-8"),
            "https://www.reddit.com/r/BuyItForLife/",
        )

        self.assertEqual(rows[0][:2], ("buyitforlife", "community"))
        self.assertEqual(rows[0][2]["subscriberCount"], 2543210)
        self.assertEqual(rows[0][2]["activeUserCount"], 1842)


if __name__ == "__main__":
    unittest.main()
