from __future__ import annotations

import json
from pathlib import Path
import unittest

from reddita.reddit_json import (
    RedditJsonError, extract_more_children_json, extract_more_children_response_json,
    parse_about_json,
    parse_listing_json, parse_morechildren_json, parse_thread_json,
)


FIXTURES = Path(__file__).parent / "fixtures"


class RedditJsonParserTests(unittest.TestCase):
    def test_morechildren_response_exposes_next_generation_ids(self):
        payload = {"json": {"errors": [], "data": {"things": [{
            "kind": "more", "data": {"children": ["next01", "next02"]},
        }]}}}
        self.assertEqual(
            extract_more_children_response_json(payload), ("next01", "next02"),
        )

    def test_listing_preserves_research_fields_and_after_cursor(self):
        rows, after = parse_listing_json(
            (FIXTURES / "json-listing-search.json").read_text(),
            "https://www.reddit.com/search.json?q=durable+chair&limit=25",
        )

        self.assertEqual(after, "t3_next99")
        self.assertEqual([row[:2] for row in rows], [
            ("self01", "post"), ("link02", "post"),
        ])
        self_post = rows[0][2]
        self.assertEqual(
            (self_post["score"], self_post["commentCount"], self_post["body"]),
            (321, 47, "The steel frame is still solid after daily use."),
        )
        self.assertIsNone(self_post["externalUrl"])
        self.assertEqual(self_post["observedFullname"], "t3_self01")
        self.assertEqual(self_post["capturePage"], "https://www.reddit.com/search.json?q=durable+chair&limit=25")
        self.assertEqual(rows[1][2]["externalUrl"], "https://example.com/chairs/model-42")
        self.assertEqual(self_post["age"], "2024-01-01T00:00:00Z")

    def test_thread_recurses_and_reports_unexpanded_or_limited_comments(self):
        rows, truncated = parse_thread_json(
            (FIXTURES / "json-thread-recursive.json").read_text(),
            "https://www.reddit.com/comments/thr123/which_kettle_lasts.json?limit=2",
            max_comments=2,
        )

        self.assertTrue(truncated)
        self.assertEqual([row[:2] for row in rows], [
            ("thr123", "thread"), ("top001", "comment"), ("reply02", "comment"),
        ])
        post = rows[0][2]
        top, reply = rows[1][2], rows[2][2]
        self.assertEqual((post["score"], post["commentCount"]), (145, 3))
        self.assertEqual((top["parentId"], top["depth"], top["topLevel"]), ("thr123", 0, True))
        self.assertEqual((reply["parentId"], reply["depth"], reply["topLevel"]), ("top001", 1, False))
        self.assertEqual(reply["score"], 11)
        self.assertEqual(reply["age"], "2024-01-01T02:00:00Z")
        self.assertEqual(reply["surface"], "structured")

    def test_thread_comment_listing_cursor_marks_capture_truncated(self):
        rows, truncated = parse_thread_json(
            (FIXTURES / "json-thread-after.json").read_text(),
            "https://www.reddit.com/comments/after1/a_bounded_comment_page.json",
            max_comments=10,
        )

        self.assertTrue(truncated)
        self.assertEqual([row[:2] for row in rows], [
            ("after1", "thread"), ("first1", "comment"),
        ])

    def test_listing_rejects_post_permalink_for_another_identifier(self):
        with self.assertRaises(RedditJsonError):
            parse_listing_json(
                (FIXTURES / "json-listing-mismatched-permalink.json").read_text(),
                "https://www.reddit.com/search.json?q=mismatched",
            )

    def test_morechildren_ids_and_response_are_bounded_with_hierarchy(self):
        child_ids = extract_more_children_json(
            (FIXTURES / "json-thread-more-ids.json").read_text(),
        )
        rows, truncated = parse_morechildren_json(
            (FIXTURES / "json-morechildren.json").read_text(),
            "https://www.reddit.com/api/morechildren.json?link_id=t3_more01",
            thread_id="more01",
            max_comments=2,
        )

        self.assertEqual(child_ids, ("top001", "child02", "later3"))
        self.assertTrue(truncated)
        self.assertEqual([row[:2] for row in rows], [
            ("top001", "comment"), ("child02", "comment"),
        ])
        self.assertEqual(
            (rows[0][2]["parentId"], rows[0][2]["depth"], rows[0][2]["topLevel"]),
            ("more01", 0, True),
        )
        self.assertEqual(
            (rows[1][2]["parentId"], rows[1][2]["depth"], rows[1][2]["topLevel"]),
            ("top001", 1, False),
        )

    def test_about_preserves_public_community_counts_and_provenance(self):
        rows = parse_about_json(
            json.loads((FIXTURES / "json-about.json").read_text()),
            "https://www.reddit.com/r/BuyItForLife/about.json",
        )

        self.assertEqual(len(rows), 1)
        identifier, record_type, community = rows[0]
        self.assertEqual((identifier, record_type), ("buyitforlife", "community"))
        self.assertEqual((community["subscriberCount"], community["activeUserCount"]), (2543210, 1842))
        self.assertEqual(community["description"], "Products made to last.")
        self.assertEqual(community["capturePage"], "https://www.reddit.com/r/BuyItForLife/about.json")


if __name__ == "__main__":
    unittest.main()
