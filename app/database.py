"""SQLite storage: dedup, collection/delivery history, runtime control state, retention.

Concurrency model: the poller and Telegram bot are separate local processes
that may open this database at the same time. WAL mode lets readers and a
writer proceed concurrently; `busy_timeout` makes SQLite block-and-retry
internally (up to the timeout) instead of immediately raising "database is
locked" on a write collision; a small bounded application-level retry sits
on top of that as a second safety net. Every write here is a single short
statement immediately followed by `commit()` — no write transaction is ever
held open across an HTTP call, a Telegram API call, or a sleep.

Dedup key priority: `source_id` (stable, from the API) first, `raw_hash`
(SHA-256 of sender + sent_at + full body) as a fallback/second check, and
finally the `tombstones` table so a record deleted by retention cleanup is
not immediately re-collected as "new" if it reappears in a poll window.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator
from zoneinfo import ZoneInfo

from app.models import DisasterMessageRecord, TelegramStatus

logger = logging.getLogger(__name__)

SEOUL_TZ = ZoneInfo("Asia/Seoul")

BUSY_TIMEOUT_MS = 5000
LOCKED_RETRY_ATTEMPTS = 3
LOCKED_RETRY_BACKOFF_SECONDS = 0.2

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
CREATE INDEX IF NOT EXISTS idx_messages_sent_at ON messages(sent_at);
CREATE INDEX IF NOT EXISTS idx_messages_detected_at ON messages(detected_at);
CREATE INDEX IF NOT EXISTS idx_messages_telegram_status ON messages(telegram_status);

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

CREATE INDEX IF NOT EXISTS idx_run_history_started_at ON run_history(started_at);
CREATE INDEX IF NOT EXISTS idx_run_history_finished_at ON run_history(finished_at);

CREATE TABLE IF NOT EXISTS tombstones (
    source_id TEXT PRIMARY KEY,
    raw_hash TEXT NOT NULL,
    expired_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tombstones_raw_hash ON tombstones(raw_hash);
CREATE INDEX IF NOT EXISTS idx_tombstones_expired_at ON tombstones(expired_at);

CREATE TABLE IF NOT EXISTS system_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    polling_enabled INTEGER NOT NULL DEFAULT 1,
    paused_at TEXT,
    paused_by INTEGER,
    resumed_at TEXT,
    resumed_by INTEGER,
    last_successful_poll_at TEXT,
    last_poll_error TEXT,
    last_new_message_at TEXT,
    last_cleanup_at TEXT
);

INSERT OR IGNORE INTO system_state (id, polling_enabled) VALUES (1, 1);
"""


@dataclass
class SystemState:
    polling_enabled: bool
    paused_at: datetime | None
    paused_by: int | None
    resumed_at: datetime | None
    resumed_by: int | None
    last_successful_poll_at: datetime | None
    last_poll_error: str | None
    last_new_message_at: datetime | None
    last_cleanup_at: datetime | None


@dataclass
class CleanupCounts:
    message_rows_eligible: int
    run_history_rows_eligible: int
    tombstone_rows_eligible: int
    post_cleanup_message_count: int
    post_cleanup_run_history_count: int


def _parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _iso(value: datetime) -> str:
    return value.isoformat()


class DatabaseLockedError(RuntimeError):
    """Raised after exhausting bounded retries on a persistent 'database is locked' error."""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), timeout=BUSY_TIMEOUT_MS / 1000)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _execute_write(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Execute + commit a single write statement, retrying briefly on lock contention."""
        last_exc: sqlite3.OperationalError | None = None
        for attempt in range(LOCKED_RETRY_ATTEMPTS):
            try:
                cur = self._conn.execute(sql, params)
                self._conn.commit()
                return cur
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower():
                    raise
                last_exc = exc
                logger.warning("database is locked (attempt %d): %s", attempt + 1, exc)
                time.sleep(LOCKED_RETRY_BACKOFF_SECONDS * (attempt + 1))
        raise DatabaseLockedError(
            f"database is locked after {LOCKED_RETRY_ATTEMPTS} attempts: {last_exc}"
        )

    # -- message records -----------------------------------------------

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
            """
            SELECT 1 FROM messages WHERE source_id = ? OR raw_hash = ?
            UNION ALL
            SELECT 1 FROM tombstones WHERE source_id = ? OR raw_hash = ?
            LIMIT 1
            """,
            (record.source_id, record.raw_hash, record.source_id, record.raw_hash),
        )
        return cur.fetchone() is not None

    def insert(self, record: DisasterMessageRecord, *, is_baseline: bool = False) -> int:
        """Insert a new record. Caller must have already checked is_known()."""
        cur = self._execute_write(
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
        internal_id = cur.lastrowid
        record.internal_id = internal_id
        return internal_id

    def update_telegram_result(
        self, internal_id: int, *, status: TelegramStatus, message_id: str | None
    ) -> None:
        self._execute_write(
            "UPDATE messages SET telegram_status = ?, telegram_message_id = ? WHERE internal_id = ?",
            (status.value, message_id, internal_id),
        )

    def pending_retry_records(self) -> list[DisasterMessageRecord]:
        """Previously collected, non-baseline records that still need a Telegram send."""
        cur = self._conn.execute(
            "SELECT * FROM messages WHERE telegram_status IN (?, ?) AND is_baseline = 0",
            (TelegramStatus.TELEGRAM_PENDING.value, TelegramStatus.TELEGRAM_FAILED.value),
        )
        return [self._row_to_record(row) for row in cur.fetchall()]

    def get_latest_record(self) -> DisasterMessageRecord | None:
        """Most recent record by source `sent_at`, `internal_id` DESC as a deterministic tiebreak."""
        cur = self._conn.execute(
            "SELECT * FROM messages ORDER BY sent_at DESC, internal_id DESC LIMIT 1"
        )
        row = cur.fetchone()
        return self._row_to_record(row) if row is not None else None

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> DisasterMessageRecord:
        # datetime.fromisoformat() reconstructs a fixed-offset tzinfo
        # (e.g. "UTC+09:00") from the stored "+09:00" string, losing the
        # original ZoneInfo("Asia/Seoul") identity — .astimezone(SEOUL_TZ)
        # re-attaches it (same instant, correct zone name) so downstream
        # formatting (e.g. strftime("%Z") in telegram replies) reads "KST"
        # instead of "UTC+09:00".
        record = DisasterMessageRecord(
            source_id=row["source_id"],
            sender_or_region=row["sender_or_region"],
            sent_at=datetime.fromisoformat(row["sent_at"]).astimezone(SEOUL_TZ),
            original_body=row["original_body"],
            source_url=row["source_url"],
            detected_at=datetime.fromisoformat(row["detected_at"]).astimezone(SEOUL_TZ),
            raw_payload=json.loads(row["raw_payload"]),
        )
        record.internal_id = row["internal_id"]
        record.telegram_status = TelegramStatus(row["telegram_status"])
        record.telegram_message_id = row["telegram_message_id"]
        return record

    # -- run history ------------------------------------------------------

    def start_run(self, run_type: str) -> int:
        cur = self._execute_write(
            "INSERT INTO run_history (run_type, started_at, status) VALUES (?, ?, ?)",
            (run_type, datetime.now(tz=SEOUL_TZ).isoformat(), "running"),
        )
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
        store_successful_noop_runs: bool = True,
    ) -> None:
        """Finish a run_history row.

        A "successful no-op" run (status ok, nothing new, nothing failed) is
        deleted instead of kept when `store_successful_noop_runs` is False,
        so routine polling doesn't grow run_history by one row per cycle
        forever. Failures, runs that found new records, and runs with
        Telegram delivery failures are always kept.
        """
        is_noop = status == "ok" and new_count == 0 and failed_count == 0
        if is_noop and not store_successful_noop_runs:
            self._execute_write("DELETE FROM run_history WHERE run_id = ?", (run_id,))
            return

        self._execute_write(
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

    def record_control_event(self, run_type: str, *, actor_user_id: int, detail: str) -> None:
        """Log a /pause or /resume action (run_type: 'pause' or 'resume') in run_history.

        Control events are always retained (never suppressed as a no-op).
        """
        now = datetime.now(tz=SEOUL_TZ).isoformat()
        self._execute_write(
            """
            INSERT INTO run_history (run_type, started_at, finished_at, status, detail)
            VALUES (?, ?, ?, 'ok', ?)
            """,
            (run_type, now, now, f"actor_user_id={actor_user_id} {detail}".strip()),
        )

    def run_history_count(self) -> int:
        cur = self._conn.execute("SELECT COUNT(*) AS c FROM run_history")
        return cur.fetchone()["c"]

    # -- runtime control state --------------------------------------------

    def get_system_state(self) -> SystemState:
        cur = self._conn.execute("SELECT * FROM system_state WHERE id = 1")
        row = cur.fetchone()
        return SystemState(
            polling_enabled=bool(row["polling_enabled"]),
            paused_at=_parse_dt(row["paused_at"]),
            paused_by=row["paused_by"],
            resumed_at=_parse_dt(row["resumed_at"]),
            resumed_by=row["resumed_by"],
            last_successful_poll_at=_parse_dt(row["last_successful_poll_at"]),
            last_poll_error=row["last_poll_error"],
            last_new_message_at=_parse_dt(row["last_new_message_at"]),
            last_cleanup_at=_parse_dt(row["last_cleanup_at"]),
        )

    def is_polling_enabled(self) -> bool:
        cur = self._conn.execute("SELECT polling_enabled FROM system_state WHERE id = 1")
        return bool(cur.fetchone()["polling_enabled"])

    def pause_polling(self, *, actor_user_id: int, now: datetime | None = None) -> bool:
        """Idempotent: returns True if this call changed state, False if already paused."""
        if not self.is_polling_enabled():
            return False
        now = now or datetime.now(tz=SEOUL_TZ)
        self._execute_write(
            "UPDATE system_state SET polling_enabled = 0, paused_at = ?, paused_by = ? WHERE id = 1",
            (_iso(now), actor_user_id),
        )
        logger.info("polling paused by user_id=%s at %s", actor_user_id, now.isoformat())
        return True

    def resume_polling(self, *, actor_user_id: int, now: datetime | None = None) -> bool:
        """Idempotent: returns True if this call changed state, False if already active."""
        if self.is_polling_enabled():
            return False
        now = now or datetime.now(tz=SEOUL_TZ)
        self._execute_write(
            "UPDATE system_state SET polling_enabled = 1, resumed_at = ?, resumed_by = ? WHERE id = 1",
            (_iso(now), actor_user_id),
        )
        logger.info("polling resumed by user_id=%s at %s", actor_user_id, now.isoformat())
        return True

    def record_poll_success(self, *, now: datetime | None = None, found_new: bool = False) -> None:
        now = now or datetime.now(tz=SEOUL_TZ)
        if found_new:
            self._execute_write(
                "UPDATE system_state SET last_successful_poll_at = ?, last_poll_error = NULL, "
                "last_new_message_at = ? WHERE id = 1",
                (_iso(now), _iso(now)),
            )
        else:
            self._execute_write(
                "UPDATE system_state SET last_successful_poll_at = ?, last_poll_error = NULL WHERE id = 1",
                (_iso(now),),
            )

    def record_poll_error(self, error: str) -> None:
        self._execute_write(
            "UPDATE system_state SET last_poll_error = ? WHERE id = 1",
            (error[:500],),
        )

    def record_cleanup_run(self, *, now: datetime | None = None) -> None:
        now = now or datetime.now(tz=SEOUL_TZ)
        self._execute_write(
            "UPDATE system_state SET last_cleanup_at = ? WHERE id = 1",
            (_iso(now),),
        )

    def should_run_cleanup(
        self, *, cleanup_interval_hours: int, now: datetime | None = None
    ) -> bool:
        now = now or datetime.now(tz=SEOUL_TZ)
        last = self.get_system_state().last_cleanup_at
        if last is None:
            return True
        return now - last >= timedelta(hours=cleanup_interval_hours)

    # -- retention / cleanup -----------------------------------------------

    def cleanup_preview(
        self,
        *,
        message_retention_days: int,
        run_history_retention_days: int,
        tombstone_retention_days: int,
        now: datetime | None = None,
    ) -> CleanupCounts:
        now = now or datetime.now(tz=SEOUL_TZ)
        message_cutoff = _iso(now - timedelta(days=message_retention_days))
        run_history_cutoff = _iso(now - timedelta(days=run_history_retention_days))
        tombstone_cutoff = _iso(now - timedelta(days=tombstone_retention_days))

        total_messages = self._conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"]
        messages_eligible = self._conn.execute(
            "SELECT COUNT(*) AS c FROM messages WHERE sent_at < ?", (message_cutoff,)
        ).fetchone()["c"]

        total_run_history = self.run_history_count()
        run_history_eligible = self._conn.execute(
            "SELECT COUNT(*) AS c FROM run_history WHERE started_at < ?", (run_history_cutoff,)
        ).fetchone()["c"]

        tombstones_eligible = self._conn.execute(
            "SELECT COUNT(*) AS c FROM tombstones WHERE expired_at < ?", (tombstone_cutoff,)
        ).fetchone()["c"]

        return CleanupCounts(
            message_rows_eligible=messages_eligible,
            run_history_rows_eligible=run_history_eligible,
            tombstone_rows_eligible=tombstones_eligible,
            post_cleanup_message_count=total_messages - messages_eligible,
            post_cleanup_run_history_count=total_run_history - run_history_eligible,
        )

    def cleanup_execute(
        self,
        *,
        message_retention_days: int,
        run_history_retention_days: int,
        tombstone_retention_days: int,
        now: datetime | None = None,
    ) -> CleanupCounts:
        """Delete expired rows. Message deletions are tombstoned first (source_id +
        raw_hash only, not the message body) so dedup keeps working across cleanup.
        Each step is its own short transaction.
        """
        now = now or datetime.now(tz=SEOUL_TZ)
        preview = self.cleanup_preview(
            message_retention_days=message_retention_days,
            run_history_retention_days=run_history_retention_days,
            tombstone_retention_days=tombstone_retention_days,
            now=now,
        )

        message_cutoff = _iso(now - timedelta(days=message_retention_days))
        run_history_cutoff = _iso(now - timedelta(days=run_history_retention_days))
        tombstone_cutoff = _iso(now - timedelta(days=tombstone_retention_days))
        expired_at = _iso(now)

        expiring = self._conn.execute(
            "SELECT source_id, raw_hash FROM messages WHERE sent_at < ?", (message_cutoff,)
        ).fetchall()
        for row in expiring:
            self._execute_write(
                "INSERT OR REPLACE INTO tombstones (source_id, raw_hash, expired_at) VALUES (?, ?, ?)",
                (row["source_id"], row["raw_hash"], expired_at),
            )
        self._execute_write("DELETE FROM messages WHERE sent_at < ?", (message_cutoff,))
        self._execute_write("DELETE FROM run_history WHERE started_at < ?", (run_history_cutoff,))
        self._execute_write("DELETE FROM tombstones WHERE expired_at < ?", (tombstone_cutoff,))

        self.record_cleanup_run(now=now)
        return preview

    # -- status / reporting --------------------------------------------------

    def status_report(self) -> dict:
        total_messages = self._conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"]
        baseline_messages = self._conn.execute(
            "SELECT COUNT(*) AS c FROM messages WHERE is_baseline = 1"
        ).fetchone()["c"]
        by_status = {
            row["telegram_status"]: row["c"]
            for row in self._conn.execute(
                "SELECT telegram_status, COUNT(*) AS c FROM messages GROUP BY telegram_status"
            ).fetchall()
        }
        oldest_newest = self._conn.execute(
            "SELECT MIN(sent_at) AS oldest, MAX(sent_at) AS newest FROM messages"
        ).fetchone()
        run_history_count = self.run_history_count()
        run_history_span = self._conn.execute(
            "SELECT MIN(started_at) AS oldest, MAX(started_at) AS newest FROM run_history"
        ).fetchone()

        return {
            "total_messages": total_messages,
            "baseline_messages": baseline_messages,
            "messages_by_telegram_status": by_status,
            "oldest_sent_at": oldest_newest["oldest"],
            "newest_sent_at": oldest_newest["newest"],
            "run_history_count": run_history_count,
            "oldest_run_history_at": run_history_span["oldest"],
            "newest_run_history_at": run_history_span["newest"],
        }


@contextmanager
def open_database(path: Path) -> Iterator[Database]:
    db = Database(path)
    try:
        yield db
    finally:
        db.close()
