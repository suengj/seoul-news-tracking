"""SQLite storage: dedup, collection/delivery history, baseline tracking.

Dedup key priority: `source_id` (stable, from the API) first, `raw_hash`
(SHA-256 of sender + sent_at + full body) as a fallback/second check. A
record already present by either key is never re-inserted or re-sent.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator
from zoneinfo import ZoneInfo

from app.models import DisasterMessageRecord, TelegramStatus

SEOUL_TZ = ZoneInfo("Asia/Seoul")

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    internal_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL UNIQUE,
    raw_hash TEXT NOT NULL,
    sender_or_region TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    original_body TEXT NOT NULL,
    source_url TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    raw_payload TEXT NOT NULL,
    telegram_status TEXT NOT NULL,
    telegram_message_id TEXT,
    is_baseline INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_messages_raw_hash ON messages(raw_hash);

CREATE TABLE IF NOT EXISTS run_history (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_type TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    fetched_count INTEGER,
    new_count INTEGER,
    duplicate_count INTEGER,
    sent_count INTEGER,
    failed_count INTEGER,
    status TEXT NOT NULL,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS template_suggestions (
    suggestion_id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL REFERENCES messages(internal_id),
    recommended_template_id TEXT,
    rule_score REAL,
    candidates_json TEXT NOT NULL,
    extraction_json TEXT NOT NULL,
    rendered_text TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_template_suggestions_message_id
    ON template_suggestions(message_id);

CREATE TABLE IF NOT EXISTS template_actions (
    action_id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL REFERENCES messages(internal_id),
    selected_template_id TEXT NOT NULL,
    selected_by INTEGER NOT NULL,
    selected_at TEXT NOT NULL,
    callback_query_id TEXT UNIQUE,
    extraction_json TEXT,
    rendered_text TEXT,
    status TEXT NOT NULL,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_template_actions_message_id
    ON template_actions(message_id);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def is_empty(self) -> bool:
        cur = self._conn.execute("SELECT COUNT(*) AS c FROM messages")
        return cur.fetchone()["c"] == 0

    def known_source_ids(self) -> set[str]:
        cur = self._conn.execute("SELECT source_id FROM messages")
        return {row["source_id"] for row in cur.fetchall()}

    def known_raw_hashes(self) -> set[str]:
        cur = self._conn.execute("SELECT raw_hash FROM messages")
        return {row["raw_hash"] for row in cur.fetchall()}

    def is_known(self, record: DisasterMessageRecord) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM messages WHERE source_id = ? OR raw_hash = ? LIMIT 1",
            (record.source_id, record.raw_hash),
        )
        return cur.fetchone() is not None

    def pending_retry_records(self) -> list[DisasterMessageRecord]:
        """Previously collected, non-baseline records that still need a Telegram send."""
        cur = self._conn.execute(
            "SELECT * FROM messages WHERE telegram_status IN (?, ?) AND is_baseline = 0",
            (TelegramStatus.TELEGRAM_PENDING.value, TelegramStatus.TELEGRAM_FAILED.value),
        )
        return [self._row_to_record(row) for row in cur.fetchall()]

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> DisasterMessageRecord:
        record = DisasterMessageRecord(
            source_id=row["source_id"],
            sender_or_region=row["sender_or_region"],
            sent_at=datetime.fromisoformat(row["sent_at"]),
            original_body=row["original_body"],
            source_url=row["source_url"],
            detected_at=datetime.fromisoformat(row["detected_at"]),
            raw_payload=json.loads(row["raw_payload"]),
        )
        record.internal_id = row["internal_id"]
        return record

    def insert(self, record: DisasterMessageRecord, *, is_baseline: bool = False) -> int:
        """Insert a new record. Caller must have already checked is_known()."""
        cur = self._conn.execute(
            """
            INSERT INTO messages (
                source_id, raw_hash, sender_or_region, sent_at, original_body,
                source_url, detected_at, raw_payload, telegram_status,
                telegram_message_id, is_baseline
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.source_id,
                record.raw_hash,
                record.sender_or_region,
                record.sent_at.isoformat(),
                record.original_body,
                record.source_url,
                record.detected_at.isoformat(),
                json.dumps(record.raw_payload, ensure_ascii=False),
                record.telegram_status.value,
                record.telegram_message_id,
                1 if is_baseline else 0,
            ),
        )
        self._conn.commit()
        internal_id = cur.lastrowid
        record.internal_id = internal_id
        return internal_id

    def update_telegram_result(
        self, internal_id: int, *, status: TelegramStatus, message_id: str | None
    ) -> None:
        self._conn.execute(
            "UPDATE messages SET telegram_status = ?, telegram_message_id = ? WHERE internal_id = ?",
            (status.value, message_id, internal_id),
        )
        self._conn.commit()

    def get_by_internal_id(self, internal_id: int) -> DisasterMessageRecord | None:
        cur = self._conn.execute("SELECT * FROM messages WHERE internal_id = ?", (internal_id,))
        row = cur.fetchone()
        return self._row_to_record(row) if row is not None else None

    def insert_template_suggestion(
        self,
        *,
        message_id: int,
        recommended_template_id: str | None,
        rule_score: float | None,
        candidates_json: str,
        extraction_json: str,
        rendered_text: str | None,
    ) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO template_suggestions (
                message_id, recommended_template_id, rule_score,
                candidates_json, extraction_json, rendered_text, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                recommended_template_id,
                rule_score,
                candidates_json,
                extraction_json,
                rendered_text,
                datetime.now(tz=SEOUL_TZ).isoformat(),
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def has_processed_callback(self, callback_query_id: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM template_actions WHERE callback_query_id = ? LIMIT 1",
            (callback_query_id,),
        )
        return cur.fetchone() is not None

    def insert_template_action(
        self,
        *,
        message_id: int,
        selected_template_id: str,
        selected_by: int,
        callback_query_id: str | None,
        extraction_json: str | None,
        rendered_text: str | None,
        status: str,
        error: str | None = None,
    ) -> int:
        """Raises `sqlite3.IntegrityError` if `callback_query_id` was already recorded
        (duplicate Telegram callback delivery) — caller should catch and skip re-sending."""
        cur = self._conn.execute(
            """
            INSERT INTO template_actions (
                message_id, selected_template_id, selected_by, selected_at,
                callback_query_id, extraction_json, rendered_text, status, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                selected_template_id,
                selected_by,
                datetime.now(tz=SEOUL_TZ).isoformat(),
                callback_query_id,
                extraction_json,
                rendered_text,
                status,
                error,
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def start_run(self, run_type: str) -> int:
        cur = self._conn.execute(
            "INSERT INTO run_history (run_type, started_at, status) VALUES (?, ?, ?)",
            (run_type, datetime.now(tz=SEOUL_TZ).isoformat(), "running"),
        )
        self._conn.commit()
        return cur.lastrowid

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        fetched_count: int = 0,
        new_count: int = 0,
        duplicate_count: int = 0,
        sent_count: int = 0,
        failed_count: int = 0,
        detail: str = "",
    ) -> None:
        self._conn.execute(
            """
            UPDATE run_history
            SET finished_at = ?, status = ?, fetched_count = ?, new_count = ?,
                duplicate_count = ?, sent_count = ?, failed_count = ?, detail = ?
            WHERE run_id = ?
            """,
            (
                datetime.now(tz=SEOUL_TZ).isoformat(),
                status,
                fetched_count,
                new_count,
                duplicate_count,
                sent_count,
                failed_count,
                detail,
                run_id,
            ),
        )
        self._conn.commit()


@contextmanager
def open_database(path: Path) -> Iterator[Database]:
    db = Database(path)
    try:
        yield db
    finally:
        db.close()
