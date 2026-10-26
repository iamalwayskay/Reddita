from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from reddita.models import CaptureEnvelope, CapturePage, CaptureRecord, SourceResult
from reddita.scraper import RedditCapture
from reddita.storage import ResearchCorpus, StorageError


NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def make_capture(
    *, title: str = "Cat supplement experiences", captured_at: datetime = NOW,
) -> RedditCapture:
    data = {
        "id": "abc123",
        "title": title,
        "body": "My senior cat improved after a month, but ask a veterinarian.",
        "author": "cat_owner",
        "subreddit": "CatAdvice",
        "permalink": "https://www.reddit.com/r/CatAdvice/comments/abc123/cat_supplement/",
        "score": 42,
        "commentCount": 17,
    }
    record = CaptureRecord(
        record_id="abc123",
        record_type="post",
        source_locator=data["permalink"],
        source_index=0,
        data_json=json.dumps(data),
    )
    result = SourceResult(
        envelope=CaptureEnvelope(
            schema_version="1",
            source="reddit",
            operation="search",
            requested_locator="cat supplements",
            final_locator="https://www.reddit.com/search/?q=cat+supplements",
            captured_at=captured_at,
            collection_mode="structured",
            complete=True,
            record_count=1,
            continuation=None,
            gaps=(),
        ),
        records=(record,),
        pages=(CapturePage(
            page_index=0,
            locator="https://www.reddit.com/search/?q=cat+supplements",
            record_ids=("abc123",),
        ),),
    )
    return RedditCapture(
        result=result,
        surface="live",
        content_digest="1" * 64,
        settings_digest="2" * 64,
        acquisition_mode="acquired",
        original_acquired_at=captured_at,
    )


class ResearchCorpusTests(unittest.TestCase):
    def test_database_is_private_versioned_and_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "corpus" / "research.sqlite3"
            with ResearchCorpus(database):
                pass

            if os.name != "nt":
                self.assertEqual(os.stat(database).st_mode & 0o777, 0o600)
            with sqlite3.connect(database) as connection:
                version = connection.execute(
                    "SELECT version FROM schema_version"
                ).fetchone()[0]
            self.assertEqual(version, 3)

            target = root / "real.sqlite3"
            target.touch()
            link = root / "linked.sqlite3"
            link.symlink_to(target)
            with self.assertRaises(StorageError):
                ResearchCorpus(link)

            linked_parent = root / "linked-parent"
            linked_parent.symlink_to(root / "corpus", target_is_directory=True)
            with self.assertRaises(StorageError):
                ResearchCorpus(linked_parent / "nested" / "research.sqlite3")

    def test_unknown_schema_version_is_rejected_without_mutating_database(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "future.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "CREATE TABLE schema_version (singleton INTEGER PRIMARY KEY, version INTEGER)"
                )
                connection.execute("INSERT INTO schema_version VALUES (1, 999)")

            with self.assertRaises(StorageError):
                ResearchCorpus(database)

            with sqlite3.connect(database) as connection:
                tables = {
                    row[0] for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            self.assertEqual(tables, {"schema_version"})

    def test_version_two_query_metadata_migrates_to_append_only_history(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "version-two.sqlite3"
            with ResearchCorpus(database) as corpus:
                receipt = corpus.save_capture(
                    make_capture(), query_metadata={"project": "legacy"},
                )
            with sqlite3.connect(database) as connection:
                connection.execute("ALTER TABLE capture_queries RENAME TO capture_queries_new")
                connection.execute(
                    """CREATE TABLE capture_queries (
                       capture_id INTEGER NOT NULL,
                       query_metadata_json TEXT NOT NULL,
                       observed_at TEXT NOT NULL,
                       PRIMARY KEY (capture_id, query_metadata_json))"""
                )
                connection.execute(
                    "INSERT INTO capture_queries(capture_id, query_metadata_json, observed_at) "
                    "SELECT capture_id, query_metadata_json, observed_at FROM capture_queries_new"
                )
                connection.execute("DROP TABLE capture_queries_new")
                connection.execute("UPDATE schema_version SET version = 2")

            with ResearchCorpus(database) as corpus:
                corpus.save_capture(make_capture(), query_metadata={"project": "again"})
                migrated = corpus.get_capture(receipt.capture_id)

            self.assertEqual(len(migrated.query_history), 2)
            self.assertEqual(migrated.query_metadata, {"project": "again"})

    def test_version_one_capture_metadata_is_backfilled_into_export_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "version-one.sqlite3"
            with ResearchCorpus(database) as corpus:
                receipt = corpus.save_capture(
                    make_capture(), query_metadata={"project": "version-one"},
                )
            with sqlite3.connect(database) as connection:
                connection.execute("DROP TABLE capture_queries")
                connection.execute("PRAGMA legacy_alter_table = ON")
                connection.execute("ALTER TABLE captures RENAME TO captures_new")
                connection.execute(
                    """CREATE TABLE captures (
                       capture_id INTEGER PRIMARY KEY, capture_key TEXT NOT NULL UNIQUE,
                       source TEXT NOT NULL, operation TEXT NOT NULL,
                       requested_locator TEXT NOT NULL, final_locator TEXT,
                       captured_at TEXT NOT NULL, collection_mode TEXT NOT NULL,
                       complete INTEGER NOT NULL, continuation TEXT, gaps_json TEXT NOT NULL,
                       pages_json TEXT NOT NULL, surface TEXT NOT NULL,
                       acquisition_mode TEXT NOT NULL, original_acquired_at TEXT NOT NULL,
                       content_digest TEXT NOT NULL, settings_digest TEXT,
                       query_metadata_json TEXT NOT NULL)"""
                )
                connection.execute(
                    """INSERT INTO captures SELECT
                       capture_id, capture_key, source, operation, requested_locator,
                       final_locator, captured_at, collection_mode, complete, continuation,
                       gaps_json, pages_json, surface, acquisition_mode,
                       original_acquired_at, content_digest, settings_digest,
                       query_metadata_json FROM captures_new"""
                )
                connection.execute("DROP TABLE captures_new")
                connection.execute("UPDATE schema_version SET version = 1")

            destination = root / "migrated.jsonl"
            with ResearchCorpus(database) as corpus:
                migrated = corpus.get_capture(receipt.capture_id)
                corpus.export_jsonl(destination)
            exported = json.loads(destination.read_text(encoding="utf-8"))

            self.assertEqual(migrated.query_metadata, {"project": "version-one"})
            self.assertEqual(len(migrated.query_history), 1)
            self.assertEqual(
                exported["provenance"][0]["queryMetadata"], {"project": "version-one"},
            )

    def test_save_is_idempotent_and_updates_one_reddit_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            with ResearchCorpus(Path(directory) / "corpus.sqlite3") as corpus:
                first = corpus.save_capture(
                    make_capture(), query_metadata={"project": "cat-health", "round": 1},
                )
                repeated = corpus.save_capture(
                    make_capture(), query_metadata={"project": "cat-health", "round": 1},
                )
                repeated_again = corpus.save_capture(
                    make_capture(), query_metadata={"project": "cat-health", "round": 3},
                )
                changed = corpus.save_capture(
                    make_capture(title="Updated cat supplement experiences"),
                    query_metadata={"project": "cat-health", "round": 2},
                )

                rows = corpus.search("updated supplement")
                capture = corpus.get_capture(changed.capture_id)
                repeated_capture = corpus.get_capture(first.capture_id)

            self.assertEqual(first.capture_id, repeated.capture_id)
            self.assertEqual(first.capture_id, repeated_again.capture_id)
            self.assertEqual(first.record_count, 1)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].record_id, "abc123")
            self.assertEqual(rows[0].data["title"], "Updated cat supplement experiences")
            self.assertEqual(rows[0].capture_count, 2)
            self.assertEqual(capture.query_metadata, {"project": "cat-health", "round": 2})
            self.assertEqual(capture.requested_locator, "cat supplements")
            self.assertTrue(capture.complete)
            self.assertEqual(len(capture.query_history), 1)
            self.assertEqual(capture.query_history[0]["queryMetadata"]["round"], 2)
            self.assertEqual(len(repeated_capture.query_history), 3)
            self.assertEqual(repeated_capture.query_metadata["round"], 3)

    def test_older_capture_cannot_replace_newer_record_state(self):
        newer = datetime(2026, 2, 1, tzinfo=timezone.utc)
        older = datetime(2025, 12, 1, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            with ResearchCorpus(Path(directory) / "corpus.sqlite3") as corpus:
                corpus.save_capture(make_capture(title="Newest evidence", captured_at=newer))
                corpus.save_capture(make_capture(title="Stale evidence", captured_at=older))
                rows = corpus.search("newest evidence")

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].data["title"], "Newest evidence")
            self.assertEqual(rows[0].first_seen_at, older.isoformat())
            self.assertEqual(rows[0].last_seen_at, newer.isoformat())

    def test_search_works_with_fts_and_safe_like_fallback(self):
        for enable_fts in (True, False):
            with self.subTest(enable_fts=enable_fts), tempfile.TemporaryDirectory() as directory:
                with ResearchCorpus(
                    Path(directory) / "corpus.sqlite3", enable_fts=enable_fts,
                ) as corpus:
                    corpus.save_capture(make_capture())
                    rows = corpus.search("senior cat", limit=5, subreddit="catadvice")
                    hostile = corpus.search("cat' OR 1=1 --")

                self.assertEqual([row.record_id for row in rows], ["abc123"])
                self.assertEqual(hostile, ())

    def test_jsonl_export_is_private_canonical_and_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "exports" / "cats.jsonl"
            with ResearchCorpus(root / "corpus.sqlite3") as corpus:
                corpus.save_capture(make_capture())
                count = corpus.export_jsonl(destination, query="veterinarian")

                target = root / "do-not-overwrite.jsonl"
                target.write_text("safe", encoding="utf-8")
                link = root / "linked.jsonl"
                link.symlink_to(target)
                with self.assertRaises(StorageError):
                    corpus.export_jsonl(link)

            self.assertEqual(count, 1)
            if os.name != "nt":
                self.assertEqual(os.stat(destination).st_mode & 0o777, 0o600)
            line = destination.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(line), 1)
            document = json.loads(line[0])
            self.assertEqual(document["source"], "reddit")
            self.assertEqual(document["recordId"], "abc123")
            self.assertEqual(document["data"]["subreddit"], "CatAdvice")
            self.assertEqual(document["provenance"][0]["operation"], "search")
            self.assertEqual(document["provenance"][0]["queryMetadata"], {})
            self.assertEqual(target.read_text(encoding="utf-8"), "safe")

    def test_public_inputs_are_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            with ResearchCorpus(Path(directory) / "corpus.sqlite3") as corpus:
                with self.assertRaises(StorageError):
                    corpus.search("x" * 513)
                with self.assertRaises(StorageError):
                    corpus.search("cat", limit=10_001)
                with self.assertRaises(StorageError):
                    corpus.save_capture(make_capture(), query_metadata={"x": "y" * 70_000})


if __name__ == "__main__":
    unittest.main()
