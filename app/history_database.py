"""SQLite storage for the raw historical backfill dataset.

Deliberately separate from `app.database.Database` (the real-time message
store) — see docs/history_database.md. Dedup key priority: `source_id`
(stable, from the `sn` URL parameter) first, `raw_hash` as a fallback/second
unique constraint for sources that lack a stable ID.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator
from zoneinfo import ZoneInfo

from app.history_models import HistoricalRawRecord

SEOUL_TZ = ZoneInfo("Asia/Seoul")

SCHEMA = """
CREATE TABLE IF NOT EXISTS historical_raw_messages (
    internal_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    source_id TEXT,
    sent_at_raw TEXT,
    sent_at TEXT,
    sender_raw TEXT,
    region_raw TEXT,
    title_raw TEXT,
    body_raw TEXT NOT NULL,
    list_url TEXT NOT NULL,
    detail_url TEXT,
    raw_payload TEXT NOT NULL,
    raw_hash TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    source_page INTEGER,
    source_position INTEGER,
    UNIQUE(source_id),
    UNIQUE(raw_hash)
);

CREATE INDEX IF NOT EXISTS idx_hist_sent_at ON historical_raw_messages(sent_at);
CREATE INDEX IF NOT EXISTS idx_hist_source_page ON historical_raw_messages(source_page);

CREATE TABLE IF NOT EXISTS historical_crawl_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_count INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    current_page INTEGER NOT NULL DEFAULT 1,
    current_cursor TEXT,
    current_date_from TEXT,
    current_date_to TEXT,
    pages_processed INTEGER NOT NULL DEFAULT 0,
    fetched_count INTEGER NOT NULL DEFAULT 0,
    inserted_count INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    malformed_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    last_error TEXT,
    updated_at TEXT NOT NULL
);
"""

RUN_STATUS_RUNNING = "running"
RUN_STATUS_PAUSED = "paused"
RUN_STATUS_COMPLETED = "completed"
RUN_STATUS_FAILED = "failed"

ACTIVE_RUN_STATUSES = (RUN_STATUS_RUNNING, RUN_STATUS_PAUSED, RUN_STATUS_FAILED)


class DuplicateRecordError(Exception):
    """Raised when a record's source_id or raw_hash already exists."""


class HistoryDatabase:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "HistoryDatabase":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def unique_count(self) -> int:
        cur = self._conn.execute("SELECT COUNT(*) AS c FROM historical_raw_messages")
        return cur.fetchone()["c"]

    def is_known_source_id(self, source_id: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM historical_raw_messages WHERE source_id = ? LIMIT 1",
            (source_id,),
        )
        return cur.fetchone() is not None

    def is_known_raw_hash(self, raw_hash: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM historical_raw_messages WHERE raw_hash = ? LIMIT 1",
            (raw_hash,),
        )
        return cur.fetchone() is not None

    def insert_record(self, record: HistoricalRawRecord) -> int:
        """Insert a new record. Raises DuplicateRecordError on a unique-constraint hit."""
        try:
            cur = self._conn.execute(
                """
                INSERT INTO historical_raw_messages (
                    source, source_id, sent_at_raw, sent_at, sender_raw, region_raw,
                    title_raw, body_raw, list_url, detail_url, raw_payload, raw_hash,
                    collected_at, source_page, source_position
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.source,
                    record.source_id,
                    record.sent_at_raw,
                    record.sent_at.isoformat() if record.sent_at else None,
                    record.sender_raw,
                    record.region_raw,
                    record.title_raw,
                    record.body_raw,
                    record.list_url,
                    record.detail_url,
                    json.dumps(record.raw_payload, ensure_ascii=False),
                    record.raw_hash,
                    datetime.now(tz=SEOUL_TZ).isoformat(),
                    record.source_page,
                    record.source_position,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise DuplicateRecordError(str(exc)) from exc
        self._conn.commit()
        internal_id = cur.lastrowid
        record.internal_id = internal_id
        return internal_id

    # -- crawl run / resume state -------------------------------------------------

    def find_active_run(self) -> sqlite3.Row | None:
        cur = self._conn.execute(
            f"""
            SELECT * FROM historical_crawl_runs
            WHERE status IN ({','.join('?' for _ in ACTIVE_RUN_STATUSES)})
            ORDER BY run_id DESC LIMIT 1
            """,
            ACTIVE_RUN_STATUSES,
        )
        return cur.fetchone()

    def latest_run(self) -> sqlite3.Row | None:
        cur = self._conn.execute(
            "SELECT * FROM historical_crawl_runs ORDER BY run_id DESC LIMIT 1"
        )
        return cur.fetchone()

    def start_run(self, *, target_count: int, current_page: int = 1) -> int:
        now = datetime.now(tz=SEOUL_TZ).isoformat()
        cur = self._conn.execute(
            """
            INSERT INTO historical_crawl_runs (
                target_count, started_at, current_page, status, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (target_count, now, current_page, RUN_STATUS_RUNNING, now),
        )
        self._conn.commit()
        return cur.lastrowid

    def update_run_progress(
        self,
        run_id: int,
        *,
        current_page: int,
        pages_processed: int,
        fetched_count: int,
        inserted_count: int,
        duplicate_count: int,
        malformed_count: int,
        error_count: int,
        retry_count: int,
        status: str = RUN_STATUS_RUNNING,
        last_error: str | None = None,
    ) -> None:
        self._conn.execute(
            """
            UPDATE historical_crawl_runs
            SET current_page = ?, pages_processed = ?, fetched_count = ?,
                inserted_count = ?, duplicate_count = ?, malformed_count = ?,
                error_count = ?, retry_count = ?, status = ?, last_error = ?,
                updated_at = ?
            WHERE run_id = ?
            """,
            (
                current_page,
                pages_processed,
                fetched_count,
                inserted_count,
                duplicate_count,
                malformed_count,
                error_count,
                retry_count,
                status,
                last_error,
                datetime.now(tz=SEOUL_TZ).isoformat(),
                run_id,
            ),
        )
        self._conn.commit()

    def finish_run(self, run_id: int, *, status: str, last_error: str | None = None) -> None:
        now = datetime.now(tz=SEOUL_TZ).isoformat()
        self._conn.execute(
            """
            UPDATE historical_crawl_runs
            SET status = ?, finished_at = ?, last_error = ?, updated_at = ?
            WHERE run_id = ?
            """,
            (status, now, last_error, now, run_id),
        )
        self._conn.commit()

    # -- validation queries --------------------------------------------------

    def validation_stats(self) -> dict:
        cur = self._conn
        row = cur.execute("SELECT COUNT(*) AS c FROM historical_raw_messages").fetchone()
        total = row["c"]

        dup_source_id = cur.execute(
            """
            SELECT COUNT(*) AS c FROM (
                SELECT source_id FROM historical_raw_messages
                WHERE source_id IS NOT NULL
                GROUP BY source_id HAVING COUNT(*) > 1
            )
            """
        ).fetchone()["c"]

        dup_raw_hash = cur.execute(
            """
            SELECT COUNT(*) AS c FROM (
                SELECT raw_hash FROM historical_raw_messages
                GROUP BY raw_hash HAVING COUNT(*) > 1
            )
            """
        ).fetchone()["c"]

        missing_body = cur.execute(
            "SELECT COUNT(*) AS c FROM historical_raw_messages WHERE body_raw IS NULL OR TRIM(body_raw) = ''"
        ).fetchone()["c"]

        missing_sent_at = cur.execute(
            "SELECT COUNT(*) AS c FROM historical_raw_messages WHERE sent_at IS NULL"
        ).fetchone()["c"]

        missing_sender = cur.execute(
            "SELECT COUNT(*) AS c FROM historical_raw_messages WHERE sender_raw IS NULL OR TRIM(sender_raw) = ''"
        ).fetchone()["c"]

        oldest = cur.execute(
            "SELECT MIN(sent_at) AS v FROM historical_raw_messages WHERE sent_at IS NOT NULL"
        ).fetchone()["v"]
        newest = cur.execute(
            "SELECT MAX(sent_at) AS v FROM historical_raw_messages WHERE sent_at IS NOT NULL"
        ).fetchone()["v"]

        page_range = cur.execute(
            "SELECT MIN(source_page) AS lo, MAX(source_page) AS hi FROM historical_raw_messages"
        ).fetchone()

        return {
            "total": total,
            "duplicate_source_id_groups": dup_source_id,
            "duplicate_raw_hash_groups": dup_raw_hash,
            "missing_body": missing_body,
            "missing_sent_at": missing_sent_at,
            "missing_sender": missing_sender,
            "oldest_sent_at": oldest,
            "newest_sent_at": newest,
            "min_source_page": page_range["lo"],
            "max_source_page": page_range["hi"],
        }

    def sample_records(self, limit: int) -> list[sqlite3.Row]:
        cur = self._conn.execute(
            "SELECT * FROM historical_raw_messages ORDER BY RANDOM() LIMIT ?",
            (limit,),
        )
        return cur.fetchall()

    def export_records(self, limit: int) -> list[sqlite3.Row]:
        cur = self._conn.execute(
            "SELECT * FROM historical_raw_messages ORDER BY internal_id LIMIT ?",
            (limit,),
        )
        return cur.fetchall()


@contextmanager
def open_history_database(path: Path) -> Iterator[HistoryDatabase]:
    db = HistoryDatabase(path)
    try:
        yield db
    finally:
        db.close()
