"""Durable, private storage for locally collected Reddit research."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from typing import Any
import uuid

from .scraper import RedditCapture


SCHEMA_VERSION = 3
MAX_QUERY_CHARS = 512
MAX_QUERY_TOKENS = 32
MAX_RESULTS = 10_000
MAX_METADATA_BYTES = 65_536
_WORD = re.compile(r"[^\W_]+", re.UNICODE)


class StorageError(ValueError):
    """The research corpus or a requested storage operation is unsafe."""


@dataclass(frozen=True, slots=True)
class SaveReceipt:
    capture_id: int
    record_count: int


@dataclass(frozen=True, slots=True)
class StoredRecord:
    source: str
    record_id: str
    record_type: str
    source_locator: str
    data: dict[str, Any]
    first_seen_at: str
    last_seen_at: str
    capture_count: int


@dataclass(frozen=True, slots=True)
class StoredCapture:
    capture_id: int
    source: str
    operation: str
    requested_locator: str
    final_locator: str | None
    captured_at: str
    complete: bool
    access_mode: str
    transport: str
    session_status: str | None
    query_metadata: dict[str, Any]
    query_history: tuple[dict[str, Any], ...]


def _canonical_metadata(value: Mapping[str, Any] | None) -> str:
    if value is None:
        return "{}"
    if not isinstance(value, Mapping):
        raise StorageError("query metadata must be an object")
    try:
        encoded = json.dumps(
            dict(value), sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError) as error:
        raise StorageError("query metadata must be finite JSON") from error
    if len(encoded.encode("utf-8")) > MAX_METADATA_BYTES:
        raise StorageError("query metadata exceeds 65536 bytes")
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):
        raise StorageError("query metadata must be an object")
    return encoded


def _prepare_private_file(path: Path) -> None:
    _reject_symlink_components(path)
    if path.exists() or path.is_symlink():
        try:
            details = path.lstat()
        except OSError as error:
            raise StorageError(f"cannot inspect storage path: {error}") from error
        if stat.S_ISLNK(details.st_mode):
            raise StorageError("storage paths must not be symbolic links")
        if not stat.S_ISREG(details.st_mode):
            raise StorageError("storage path must be a regular file")
        try:
            path.chmod(0o600)
        except OSError as error:
            raise StorageError(f"cannot make storage file private: {error}") from error
        return

    parent = path.parent
    if parent.exists() and parent.is_symlink():
        raise StorageError("storage directories must not be symbolic links")
    try:
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600,
        )
        os.close(descriptor)
    except OSError as error:
        raise StorageError(f"cannot create private storage file: {error}") from error


def _reject_symlink_components(path: Path) -> None:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        try:
            details = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise StorageError(f"cannot inspect storage path: {error}") from error
        if stat.S_ISLNK(details.st_mode):
            # macOS exposes system locations such as /var through immutable,
            # root-owned aliases.  Permit only those OS-managed components;
            # user-owned or writable-parent links remain forbidden.
            try:
                parent = current.parent.stat()
            except OSError as error:
                raise StorageError(f"cannot inspect storage path: {error}") from error
            system_alias = (
                getattr(details, "st_uid", -1) == 0
                and getattr(parent, "st_uid", -1) == 0
                and parent.st_mode & 0o022 == 0
            )
            if not system_alias:
                raise StorageError("storage paths must not contain symbolic links")


def _record_key(source: str, record_type: str, record_id: str) -> str:
    identity = "\0".join((source, record_type, record_id)).encode("utf-8")
    return hashlib.sha256(identity).hexdigest()


def _searchable_text(data: Mapping[str, Any]) -> str:
    preferred = ("title", "body", "text", "selftext", "author", "subreddit")
    values: list[str] = []
    for name in preferred:
        value = data.get(name)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            values.append(str(value))
    return "\n".join(values)


class ResearchCorpus:
    """A SQLite corpus of captures and deduplicated Reddit records.

    ``enable_fts=False`` forces the portable LIKE search backend. The default
    and ``True`` use FTS5 when the local SQLite build provides it, while still
    falling back safely when it does not.
    """

    def __init__(self, path: Path, *, enable_fts: bool | None = None) -> None:
        self.path = Path(path)
        _prepare_private_file(self.path)
        try:
            self._connection = sqlite3.connect(self.path)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA journal_mode = DELETE")
            self._initialize_schema(enable_fts is not False)
        except (OSError, sqlite3.Error, StorageError) as error:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
                self._connection = None
            if isinstance(error, StorageError):
                raise
            raise StorageError(f"cannot open research corpus: {error}") from error
        self.path.chmod(0o600)

    def __enter__(self) -> ResearchCorpus:
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()

    def close(self) -> None:
        connection = getattr(self, "_connection", None)
        if connection is not None:
            connection.close()
            self._connection = None
        if self.path.exists() and not self.path.is_symlink():
            self.path.chmod(0o600)

    def _initialize_schema(self, try_fts: bool) -> None:
        connection = self._connection
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_version (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                version INTEGER NOT NULL
            )
            """
        )
        row = connection.execute(
            "SELECT version FROM schema_version WHERE singleton = 1"
        ).fetchone()
        if row is not None and row[0] not in {1, 2, SCHEMA_VERSION}:
            raise StorageError(
                f"unsupported corpus schema version {row[0]} (expected {SCHEMA_VERSION})"
            )
        prior_version = row[0] if row is not None else None
        if row is None:
            connection.execute(
                "INSERT INTO schema_version(singleton, version) VALUES (1, ?)",
                (SCHEMA_VERSION,),
            )
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS captures (
                capture_id INTEGER PRIMARY KEY,
                capture_key TEXT NOT NULL UNIQUE,
                source TEXT NOT NULL,
                operation TEXT NOT NULL,
                requested_locator TEXT NOT NULL,
                final_locator TEXT,
                captured_at TEXT NOT NULL,
                collection_mode TEXT NOT NULL,
                complete INTEGER NOT NULL CHECK (complete IN (0, 1)),
                continuation TEXT,
                gaps_json TEXT NOT NULL,
                pages_json TEXT NOT NULL,
                surface TEXT NOT NULL,
                acquisition_mode TEXT NOT NULL,
                original_acquired_at TEXT NOT NULL,
                content_digest TEXT NOT NULL,
                settings_digest TEXT,
                access_mode TEXT NOT NULL DEFAULT 'feed',
                transport TEXT NOT NULL DEFAULT 'atom',
                session_status TEXT,
                query_metadata_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS records (
                record_key TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                record_type TEXT NOT NULL,
                record_id TEXT NOT NULL,
                source_locator TEXT NOT NULL,
                source_index INTEGER NOT NULL,
                subreddit TEXT,
                data_json TEXT NOT NULL,
                search_text TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                UNIQUE (source, record_type, record_id)
            );
            CREATE TABLE IF NOT EXISTS capture_records (
                capture_id INTEGER NOT NULL REFERENCES captures(capture_id) ON DELETE CASCADE,
                record_key TEXT NOT NULL REFERENCES records(record_key) ON DELETE CASCADE,
                source_index INTEGER NOT NULL,
                PRIMARY KEY (capture_id, record_key)
            );
            CREATE TABLE IF NOT EXISTS capture_queries (
                query_event_id INTEGER PRIMARY KEY,
                capture_id INTEGER NOT NULL REFERENCES captures(capture_id) ON DELETE CASCADE,
                query_metadata_json TEXT NOT NULL,
                observed_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS records_subreddit ON records(subreddit);
            CREATE INDEX IF NOT EXISTS records_last_seen ON records(last_seen_at DESC);
            """
        )
        if prior_version == 1:
            connection.execute("ALTER TABLE captures ADD COLUMN access_mode TEXT NOT NULL DEFAULT 'feed'")
            connection.execute("ALTER TABLE captures ADD COLUMN transport TEXT NOT NULL DEFAULT 'atom'")
            connection.execute("ALTER TABLE captures ADD COLUMN session_status TEXT")
        if prior_version in {1, 2}:
            columns = {
                item[1] for item in connection.execute("PRAGMA table_info(capture_queries)")
            }
            if "query_event_id" not in columns:
                connection.executescript(
                    """
                    ALTER TABLE capture_queries RENAME TO capture_queries_legacy;
                    CREATE TABLE capture_queries (
                        query_event_id INTEGER PRIMARY KEY,
                        capture_id INTEGER NOT NULL REFERENCES captures(capture_id) ON DELETE CASCADE,
                        query_metadata_json TEXT NOT NULL,
                        observed_at TEXT NOT NULL
                    );
                    INSERT INTO capture_queries(capture_id, query_metadata_json, observed_at)
                    SELECT capture_id, query_metadata_json, observed_at
                    FROM capture_queries_legacy;
                    DROP TABLE capture_queries_legacy;
                    """
                )
        if prior_version == 1:
            connection.execute(
                """
                INSERT INTO capture_queries(capture_id, query_metadata_json, observed_at)
                SELECT c.capture_id, c.query_metadata_json, c.captured_at
                FROM captures c
                WHERE NOT EXISTS (
                    SELECT 1 FROM capture_queries q
                    WHERE q.capture_id = c.capture_id
                      AND q.query_metadata_json = c.query_metadata_json
                )
                """
            )
        if prior_version in {1, 2}:
            connection.execute(
                "UPDATE schema_version SET version = ? WHERE singleton = 1",
                (SCHEMA_VERSION,),
            )
        self._fts = False
        if try_fts:
            try:
                connection.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS records_fts "
                    "USING fts5(record_key UNINDEXED, search_text)"
                )
                connection.execute("DELETE FROM records_fts")
                connection.execute(
                    "INSERT INTO records_fts(record_key, search_text) "
                    "SELECT record_key, search_text FROM records"
                )
                self._fts = True
            except sqlite3.OperationalError:
                self._fts = False
        connection.commit()

    def save_capture(
        self,
        capture: RedditCapture,
        *,
        query_metadata: Mapping[str, Any] | None = None,
    ) -> SaveReceipt:
        metadata_json = _canonical_metadata(query_metadata)
        capture_json = json.dumps({
            "result": capture.result.model_dump(mode="json"),
            "surface": capture.surface,
            "contentDigest": capture.content_digest,
            "settingsDigest": capture.settings_digest,
            "accessMode": capture.access_mode,
            "transport": capture.transport,
            "sessionStatus": capture.session_status,
            "originalAcquiredAt": capture.original_acquired_at.isoformat(),
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        capture_key = hashlib.sha256(capture_json.encode("utf-8")).hexdigest()
        envelope = capture.result.envelope
        captured_at = envelope.captured_at.isoformat()
        gaps_json = json.dumps(
            [gap.model_dump(mode="json") for gap in envelope.gaps],
            sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        )
        pages_json = json.dumps(
            [page.model_dump(mode="json") for page in capture.result.pages],
            sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        )
        values = (
            capture_key, envelope.source.value, envelope.operation.value,
            envelope.requested_locator, envelope.final_locator, captured_at,
            envelope.collection_mode, int(envelope.complete), envelope.continuation,
            gaps_json, pages_json, capture.surface, capture.acquisition_mode,
            capture.original_acquired_at.isoformat(), capture.content_digest,
            capture.settings_digest, capture.access_mode, capture.transport,
            capture.session_status, metadata_json,
        )
        connection = self._require_open()
        with connection:
            connection.execute(
                """
                INSERT INTO captures(
                    capture_key, source, operation, requested_locator, final_locator,
                    captured_at, collection_mode, complete, continuation, gaps_json,
                    pages_json, surface, acquisition_mode, original_acquired_at,
                    content_digest, settings_digest, access_mode, transport,
                    session_status, query_metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(capture_key) DO NOTHING
                """,
                values,
            )
            capture_id = connection.execute(
                "SELECT capture_id FROM captures WHERE capture_key = ?", (capture_key,)
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO capture_queries(capture_id, query_metadata_json, observed_at) "
                "VALUES (?, ?, ?)",
                (capture_id, metadata_json, datetime.now(timezone.utc).isoformat()),
            )
            for record in capture.result.records:
                data = json.loads(record.data_json)
                key = _record_key(envelope.source.value, record.record_type, record.record_id)
                subreddit = data.get("subreddit")
                subreddit = subreddit.casefold() if isinstance(subreddit, str) else None
                search_text = _searchable_text(data)
                connection.execute(
                    """
                    INSERT INTO records(
                        record_key, source, record_type, record_id, source_locator,
                        source_index, subreddit, data_json, search_text,
                        first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source, record_type, record_id) DO UPDATE SET
                        source_locator = CASE WHEN excluded.last_seen_at >= records.last_seen_at
                            THEN excluded.source_locator ELSE records.source_locator END,
                        source_index = CASE WHEN excluded.last_seen_at >= records.last_seen_at
                            THEN excluded.source_index ELSE records.source_index END,
                        subreddit = CASE WHEN excluded.last_seen_at >= records.last_seen_at
                            THEN excluded.subreddit ELSE records.subreddit END,
                        data_json = CASE WHEN excluded.last_seen_at >= records.last_seen_at
                            THEN excluded.data_json ELSE records.data_json END,
                        search_text = CASE WHEN excluded.last_seen_at >= records.last_seen_at
                            THEN excluded.search_text ELSE records.search_text END,
                        first_seen_at = min(records.first_seen_at, excluded.first_seen_at),
                        last_seen_at = max(records.last_seen_at, excluded.last_seen_at)
                    """,
                    (
                        key, envelope.source.value, record.record_type, record.record_id,
                        record.source_locator, record.source_index, subreddit,
                        record.data_json, search_text, captured_at, captured_at,
                    ),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO capture_records(capture_id, record_key, source_index) "
                    "VALUES (?, ?, ?)",
                    (capture_id, key, record.source_index),
                )
                if self._fts:
                    indexed_text = connection.execute(
                        "SELECT search_text FROM records WHERE record_key = ?", (key,)
                    ).fetchone()[0]
                    connection.execute("DELETE FROM records_fts WHERE record_key = ?", (key,))
                    connection.execute(
                        "INSERT INTO records_fts(record_key, search_text) VALUES (?, ?)",
                        (key, indexed_text),
                    )
        return SaveReceipt(capture_id=capture_id, record_count=len(capture.result.records))

    def get_capture(self, capture_id: int) -> StoredCapture:
        if isinstance(capture_id, bool) or not isinstance(capture_id, int) or capture_id < 1:
            raise StorageError("capture ID must be a positive integer")
        row = self._require_open().execute(
            """
            SELECT capture_id, source, operation, requested_locator, final_locator,
                   captured_at, complete, access_mode, transport, session_status,
                   query_metadata_json
            FROM captures WHERE capture_id = ?
            """,
            (capture_id,),
        ).fetchone()
        if row is None:
            raise StorageError("capture was not found")
        events = self._require_open().execute(
            """
            SELECT query_metadata_json, observed_at FROM capture_queries
            WHERE capture_id = ? ORDER BY query_event_id
            """,
            (capture_id,),
        ).fetchall()
        history = tuple({
            "queryMetadata": json.loads(event["query_metadata_json"]),
            "observedAt": event["observed_at"],
        } for event in events)
        latest_metadata = history[-1]["queryMetadata"] if history else json.loads(row["query_metadata_json"])
        return StoredCapture(
            capture_id=row["capture_id"], source=row["source"], operation=row["operation"],
            requested_locator=row["requested_locator"], final_locator=row["final_locator"],
            captured_at=row["captured_at"], complete=bool(row["complete"]),
            access_mode=row["access_mode"], transport=row["transport"],
            session_status=row["session_status"],
            query_metadata=latest_metadata, query_history=history,
        )

    def search(
        self,
        query: str,
        *,
        limit: int = 50,
        subreddit: str | None = None,
        record_types: Sequence[str] | None = None,
    ) -> tuple[StoredRecord, ...]:
        if not isinstance(query, str) or len(query) > MAX_QUERY_CHARS:
            raise StorageError("search query must be at most 512 characters")
        if not query.strip():
            return ()
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_RESULTS:
            raise StorageError("search limit must be between 1 and 10000")
        tokens = _WORD.findall(query.casefold())[:MAX_QUERY_TOKENS]
        if not tokens:
            return ()
        clauses: list[str] = []
        values: list[Any] = []
        if subreddit is not None:
            if not isinstance(subreddit, str) or not 1 <= len(subreddit) <= 64:
                raise StorageError("subreddit filter must be between 1 and 64 characters")
            clauses.append("r.subreddit = ?")
            values.append(subreddit.casefold())
        types = tuple(record_types or ())
        if len(types) > 16 or any(not isinstance(item, str) or not 1 <= len(item) <= 64 for item in types):
            raise StorageError("record type filters are invalid")
        if types:
            clauses.append("r.record_type IN (%s)" % ",".join("?" for _ in types))
            values.extend(types)

        if self._fts:
            match = " AND ".join('"' + token.replace('"', '""') + '"' for token in tokens)
            where = (" AND " + " AND ".join(clauses)) if clauses else ""
            sql = (
                "SELECT r.*, (SELECT COUNT(*) FROM capture_records cr "
                "WHERE cr.record_key = r.record_key) AS capture_count "
                "FROM records_fts f JOIN records r ON r.record_key = f.record_key "
                "WHERE records_fts MATCH ?" + where +
                " ORDER BY bm25(records_fts), r.last_seen_at DESC LIMIT ?"
            )
            parameters = [match, *values, limit]
        else:
            like_clauses = ["lower(r.search_text) LIKE ? ESCAPE '\\'" for _ in tokens]
            escaped = [
                "%" + token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                for token in tokens
            ]
            all_clauses = like_clauses + clauses
            sql = (
                "SELECT r.*, (SELECT COUNT(*) FROM capture_records cr "
                "WHERE cr.record_key = r.record_key) AS capture_count "
                "FROM records r WHERE " + " AND ".join(all_clauses) +
                " ORDER BY r.last_seen_at DESC LIMIT ?"
            )
            parameters = [*escaped, *values, limit]
        rows = self._require_open().execute(sql, parameters).fetchall()
        return tuple(self._stored_record(row) for row in rows)

    def export_jsonl(
        self,
        destination: Path,
        *,
        query: str | None = None,
        limit: int = MAX_RESULTS,
    ) -> int:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_RESULTS:
            raise StorageError("export limit must be between 1 and 10000")
        if query is None:
            rows = self._require_open().execute(
                """
                SELECT r.*, (SELECT COUNT(*) FROM capture_records cr
                              WHERE cr.record_key = r.record_key) AS capture_count
                FROM records r ORDER BY r.last_seen_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
            records = tuple(self._stored_record(row) for row in rows)
        else:
            records = self.search(query, limit=limit)

        path = Path(destination)
        _reject_symlink_components(path)
        if path.exists() or path.is_symlink():
            try:
                if stat.S_ISLNK(path.lstat().st_mode):
                    raise StorageError("export destination must not be a symbolic link")
                if not path.is_file():
                    raise StorageError("export destination must be a regular file")
            except OSError as error:
                raise StorageError(f"cannot inspect export destination: {error}") from error
        if path.parent.exists() and path.parent.is_symlink():
            raise StorageError("export directory must not be a symbolic link")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
        descriptor = None
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
                descriptor = None
                for record in records:
                    document = {
                        "source": record.source,
                        "recordType": record.record_type,
                        "recordId": record.record_id,
                        "sourceLocator": record.source_locator,
                        "data": record.data,
                        "firstSeenAt": record.first_seen_at,
                        "lastSeenAt": record.last_seen_at,
                        "captureCount": record.capture_count,
                        "provenance": self._record_provenance(
                            _record_key(record.source, record.record_type, record.record_id),
                        ),
                    }
                    output.write(json.dumps(
                        document, sort_keys=True, separators=(",", ":"),
                        ensure_ascii=False, allow_nan=False,
                    ) + "\n")
                output.flush()
                os.fsync(output.fileno())
            if path.is_symlink():
                raise StorageError("export destination must not be a symbolic link")
            os.replace(temporary, path)
            path.chmod(0o600)
        except (OSError, TypeError, ValueError) as error:
            if isinstance(error, StorageError):
                raise
            raise StorageError(f"cannot export research corpus: {error}") from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        return len(records)

    def _record_provenance(self, record_key: str) -> list[dict[str, Any]]:
        rows = self._require_open().execute(
            """
            SELECT c.capture_id, c.operation, c.requested_locator, c.captured_at,
                   c.access_mode, c.transport, q.query_metadata_json, q.observed_at
            FROM capture_records cr
            JOIN captures c ON c.capture_id = cr.capture_id
            JOIN capture_queries q ON q.capture_id = c.capture_id
            WHERE cr.record_key = ?
            ORDER BY q.query_event_id
            """,
            (record_key,),
        ).fetchall()
        return [{
            "captureId": row["capture_id"],
            "operation": row["operation"],
            "requestedLocator": row["requested_locator"],
            "capturedAt": row["captured_at"],
            "accessMode": row["access_mode"],
            "transport": row["transport"],
            "queryMetadata": json.loads(row["query_metadata_json"]),
            "observedAt": row["observed_at"],
        } for row in rows]

    def _stored_record(self, row: sqlite3.Row) -> StoredRecord:
        return StoredRecord(
            source=row["source"], record_id=row["record_id"],
            record_type=row["record_type"], source_locator=row["source_locator"],
            data=json.loads(row["data_json"]), first_seen_at=row["first_seen_at"],
            last_seen_at=row["last_seen_at"], capture_count=row["capture_count"],
        )

    def _require_open(self) -> sqlite3.Connection:
        connection = getattr(self, "_connection", None)
        if connection is None:
            raise StorageError("research corpus is closed")
        return connection


__all__ = (
    "ResearchCorpus", "SaveReceipt", "StorageError", "StoredCapture", "StoredRecord",
)
