"""SQLite storage: dedup, collection/delivery history, runtime control state,
retention, and the Service v1 human-in-the-loop template flow.

Concurrency model: the poller and Telegram bot are separate local processes
that may open this database at the same time. WAL mode lets readers and a
writer proceed concurrently; `busy_timeout` makes SQLite block-and-retry
internally (up to the timeout) instead of immediately raising "database is
locked" on a write collision; a small bounded application-level retry sits
on top of that as a second safety net. Every write here is a single short
statement immediately followed by `commit()` — no write transaction is ever
held open across an HTTP call, a Telegram API call, an OpenAI API call, or
a sleep.

Dedup key priority: `source_id` (stable, from the API) first, `raw_hash`
(SHA-256 of sender + sent_at + full body) as a fallback/second check, and
finally the `tombstones` table so a record deleted by retention cleanup is
not immediately re-collected as "new" if it reappears in a poll window.

`message_id` columns on the template_* / ai_generations tables are
deliberately plain indexed integers, not `REFERENCES messages(...)` foreign
keys: a confirmed `template_decisions` row (the future automation ground
truth) must outlive `messages` retention cleanup, and with
`PRAGMA foreign_keys=ON` an enforced FK would make `cleanup_execute()`
raise instead of deleting the expired message. See docs/database_retention.md.
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
    last_cleanup_at TEXT,
    telegram_update_offset INTEGER NOT NULL DEFAULT 0
);

INSERT OR IGNORE INTO system_state (id, polling_enabled) VALUES (1, 1);

-- Deterministic rule-engine output, computed for every new message. Shown
-- only as a secondary "실험적 추천" line — never auto-selected or auto-sent.
CREATE TABLE IF NOT EXISTS template_suggestions (
    suggestion_id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL,
    recommended_template_id TEXT,
    rule_score REAL,
    candidates_json TEXT NOT NULL,
    extraction_json TEXT NOT NULL,
    rendered_text TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_template_suggestions_message_id
    ON template_suggestions(message_id);

-- One row per template-selection button press (the raw operator action,
-- logged before a preview is built from it).
CREATE TABLE IF NOT EXISTS template_actions (
    action_id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL,
    selected_template_id TEXT NOT NULL,
    selected_by INTEGER NOT NULL,
    selected_at TEXT NOT NULL,
    callback_query_id TEXT UNIQUE,
    extraction_json TEXT,
    rendered_text TEXT,
    status TEXT NOT NULL,
    error TEXT,
    interaction_chat_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_template_actions_message_id
    ON template_actions(message_id);

-- One row per preview shown to the operator (Rule- or AI-generated). This
-- is the addressable object confirm/cancel/AI callbacks act on.
CREATE TABLE IF NOT EXISTS template_previews (
    preview_id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL,
    selected_template_id TEXT NOT NULL,
    selected_by INTEGER NOT NULL,
    extraction_method TEXT NOT NULL,
    extracted_slots_json TEXT NOT NULL,
    rendered_text TEXT,
    missing_slots_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    ai_generation_id INTEGER,
    interaction_chat_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_template_previews_message_id
    ON template_previews(message_id);

-- The authoritative, future-automation ground truth: one row per source
-- message PER OPERATOR+CHAT (v0.4.0), written only by an explicit "최종 OK".
-- Survives message retention cleanup (see module docstring). Each authorized
-- operator's decision is independent: UNIQUE(message_id, confirmed_by,
-- interaction_chat_id) so one operator's Final OK never overwrites another's.
CREATE TABLE IF NOT EXISTS template_decisions (
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL,
    preview_id INTEGER NOT NULL,
    final_template_id TEXT NOT NULL,
    final_slots_json TEXT NOT NULL,
    final_rendered_text TEXT NOT NULL,
    generation_method TEXT NOT NULL,
    confirmed_by INTEGER NOT NULL,
    confirmed_at TEXT NOT NULL,
    interaction_chat_id TEXT NOT NULL DEFAULT 'legacy',
    source_id_snapshot TEXT,
    sender_or_region_snapshot TEXT,
    sent_at_snapshot TEXT,
    original_body_snapshot TEXT,
    UNIQUE(message_id, confirmed_by, interaction_chat_id)
);

CREATE INDEX IF NOT EXISTS idx_template_decisions_message_id
    ON template_decisions(message_id);

-- One row per "AI로 작성" attempt (never stores the API key).
CREATE TABLE IF NOT EXISTS ai_generations (
    ai_generation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL,
    selected_template_id TEXT NOT NULL,
    requested_by INTEGER NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    request_slots_json TEXT NOT NULL,
    response_json TEXT,
    validated_slots_json TEXT,
    status TEXT NOT NULL,
    error TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    estimated_cost REAL,
    created_at TEXT NOT NULL,
    interaction_chat_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_ai_generations_message_id
    ON ai_generations(message_id);

-- Generalized duplicate-callback guard, checked before ANY callback
-- (template selection, confirm, cancel, AI) does any work.
CREATE TABLE IF NOT EXISTS processed_callback_queries (
    callback_query_id TEXT PRIMARY KEY,
    processed_at TEXT NOT NULL
);

-- v0.4.0: one equal, independent personal subscription per authorized
-- operator. status active|muted|unsubscribed; only private chats may be
-- active. No row is a "primary" or "default" recipient — active rows are
-- equal peers (see docs/independent_operator_model.md).
CREATE TABLE IF NOT EXISTS telegram_subscriptions (
    subscription_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL UNIQUE,
    chat_id TEXT NOT NULL UNIQUE,
    chat_type TEXT NOT NULL,
    status TEXT NOT NULL,
    registration_source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_telegram_subscriptions_status
    ON telegram_subscriptions(status);

-- v0.4.0: per-recipient automatic-delivery state — the source of truth for
-- personal automatic delivery/retry. One row per (message, subscription);
-- one recipient's failure never affects another's. No FK to messages so a
-- retention cleanup of the source message can never be blocked.
CREATE TABLE IF NOT EXISTS telegram_deliveries (
    delivery_id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL,
    subscription_id INTEGER NOT NULL,
    user_id_snapshot INTEGER NOT NULL,
    chat_id_snapshot TEXT NOT NULL,
    status TEXT NOT NULL,
    telegram_message_id TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL,
    last_attempt_at TEXT,
    sent_at TEXT,
    UNIQUE(message_id, subscription_id)
);

CREATE INDEX IF NOT EXISTS idx_telegram_deliveries_message_id
    ON telegram_deliveries(message_id);
CREATE INDEX IF NOT EXISTS idx_telegram_deliveries_status
    ON telegram_deliveries(status);
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


@dataclass
class TemplatePreview:
    preview_id: int
    message_id: int
    selected_template_id: str
    selected_by: int
    extraction_method: str
    extracted_slots_json: str
    rendered_text: str | None
    missing_slots_json: str
    status: str
    created_at: datetime
    updated_at: datetime
    ai_generation_id: int | None
    interaction_chat_id: str | None


@dataclass
class Subscription:
    subscription_id: int
    user_id: int
    chat_id: str
    chat_type: str
    status: str
    registration_source: str
    created_at: datetime
    updated_at: datetime
    last_seen_at: datetime


def _parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _iso(value: datetime) -> str:
    return value.isoformat()


def _looks_like_private_chat(chat_id: str | int | None) -> bool:
    """A Telegram private chat id is a positive integer; group/supergroup
    ids are negative. Used only by subscription seeding so a negative
    group id is never seeded and an owner is never guessed."""
    if chat_id is None:
        return False
    try:
        return int(str(chat_id).strip()) > 0
    except (TypeError, ValueError):
        return False


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
        self._migrate_schema()

    def _migrate_schema(self) -> None:
        """`CREATE TABLE IF NOT EXISTS` only helps brand-new databases; a
        database created before the decision-snapshot columns existed needs
        them added explicitly. Existing rows get NULL snapshots; every newly
        confirmed decision always populates all four columns."""
        existing_columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(template_decisions)").fetchall()
        }
        snapshot_columns = (
            "source_id_snapshot",
            "sender_or_region_snapshot",
            "sent_at_snapshot",
            "original_body_snapshot",
        )
        for column in snapshot_columns:
            if column not in existing_columns:
                self._conn.execute(f"ALTER TABLE template_decisions ADD COLUMN {column} TEXT")

        preview_columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(template_previews)").fetchall()
        }
        if "interaction_chat_id" not in preview_columns:
            self._conn.execute("ALTER TABLE template_previews ADD COLUMN interaction_chat_id TEXT")

        state_columns = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(system_state)").fetchall()
        }
        if "telegram_update_offset" not in state_columns:
            self._conn.execute(
                "ALTER TABLE system_state ADD COLUMN telegram_update_offset INTEGER NOT NULL DEFAULT 0"
            )

        # v0.4.0: nullable chat-attribution columns for independent auditability.
        action_columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(template_actions)").fetchall()
        }
        if "interaction_chat_id" not in action_columns:
            self._conn.execute("ALTER TABLE template_actions ADD COLUMN interaction_chat_id TEXT")

        ai_columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(ai_generations)").fetchall()
        }
        if "interaction_chat_id" not in ai_columns:
            self._conn.execute("ALTER TABLE ai_generations ADD COLUMN interaction_chat_id TEXT")

        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_template_previews_owner "
            "ON template_previews(message_id, selected_by, interaction_chat_id, status)"
        )

        self._conn.commit()

        # v0.4.0: rebuild template_decisions to drop the old UNIQUE(message_id)
        # and adopt UNIQUE(message_id, confirmed_by, interaction_chat_id) so
        # each operator's Final OK is independent. SQLite cannot ALTER away a
        # UNIQUE constraint, so a compact table rebuild is required.
        decision_columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(template_decisions)").fetchall()
        }
        if "interaction_chat_id" not in decision_columns:
            self._rebuild_template_decisions()

    def _rebuild_template_decisions(self) -> None:
        """Migrate template_decisions to the per-operator schema, preserving
        every existing decision and its immutable source snapshots.

        `interaction_chat_id` is recovered from the confirming preview
        (`template_previews.preview_id`); a decision whose preview has no
        recorded chat (pre-v0.3.0 rows) gets the deterministic marker
        `'legacy'` instead of being discarded. Idempotent: it first drops any
        leftover `template_decisions_v2` from an interrupted run.
        """
        logger.info("migrating template_decisions to per-operator schema (v0.4.0)")
        self._conn.execute("DROP TABLE IF EXISTS template_decisions_v2")
        self._conn.execute(
            """
            CREATE TABLE template_decisions_v2 (
                decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL,
                preview_id INTEGER NOT NULL,
                final_template_id TEXT NOT NULL,
                final_slots_json TEXT NOT NULL,
                final_rendered_text TEXT NOT NULL,
                generation_method TEXT NOT NULL,
                confirmed_by INTEGER NOT NULL,
                confirmed_at TEXT NOT NULL,
                interaction_chat_id TEXT NOT NULL DEFAULT 'legacy',
                source_id_snapshot TEXT,
                sender_or_region_snapshot TEXT,
                sent_at_snapshot TEXT,
                original_body_snapshot TEXT,
                UNIQUE(message_id, confirmed_by, interaction_chat_id)
            )
            """
        )
        self._conn.execute(
            """
            INSERT INTO template_decisions_v2 (
                decision_id, message_id, preview_id, final_template_id,
                final_slots_json, final_rendered_text, generation_method,
                confirmed_by, confirmed_at, interaction_chat_id,
                source_id_snapshot, sender_or_region_snapshot,
                sent_at_snapshot, original_body_snapshot
            )
            SELECT
                d.decision_id, d.message_id, d.preview_id, d.final_template_id,
                d.final_slots_json, d.final_rendered_text, d.generation_method,
                d.confirmed_by, d.confirmed_at,
                COALESCE(p.interaction_chat_id, 'legacy'),
                d.source_id_snapshot, d.sender_or_region_snapshot,
                d.sent_at_snapshot, d.original_body_snapshot
            FROM template_decisions d
            LEFT JOIN template_previews p ON p.preview_id = d.preview_id
            """
        )
        self._conn.execute("DROP TABLE template_decisions")
        self._conn.execute("ALTER TABLE template_decisions_v2 RENAME TO template_decisions")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_template_decisions_message_id "
            "ON template_decisions(message_id)"
        )
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

    def get_recent_records(self, limit: int = 10) -> list[DisasterMessageRecord]:
        """Most recent stored messages for `/history` (excludes baseline rows).

        Ordered by source `sent_at` DESC, then `internal_id` DESC. `limit` is
        clamped to [1, 20].
        """
        bounded = max(1, min(int(limit), 20))
        cur = self._conn.execute(
            """
            SELECT * FROM messages
            WHERE is_baseline = 0
            ORDER BY sent_at DESC, internal_id DESC
            LIMIT ?
            """,
            (bounded,),
        )
        return [self._row_to_record(row) for row in cur.fetchall()]

    def get_by_internal_id(self, internal_id: int) -> DisasterMessageRecord | None:
        cur = self._conn.execute("SELECT * FROM messages WHERE internal_id = ?", (internal_id,))
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
    #
    # Only `messages`, `run_history`, and `tombstones` are pruned here.
    # template_suggestions/template_actions/template_previews/
    # template_decisions/ai_generations are never touched by cleanup — a
    # confirmed decision in particular must outlive the source message's
    # retention window (see module docstring and docs/database_retention.md).

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

        preview_count = self._conn.execute(
            "SELECT COUNT(*) AS c FROM template_previews"
        ).fetchone()["c"]
        decision_count = self._conn.execute(
            "SELECT COUNT(*) AS c FROM template_decisions"
        ).fetchone()["c"]
        ai_generation_count = self._conn.execute(
            "SELECT COUNT(*) AS c FROM ai_generations"
        ).fetchone()["c"]

        return {
            "total_messages": total_messages,
            "baseline_messages": baseline_messages,
            "messages_by_telegram_status": by_status,
            "oldest_sent_at": oldest_newest["oldest"],
            "newest_sent_at": oldest_newest["newest"],
            "run_history_count": run_history_count,
            "oldest_run_history_at": run_history_span["oldest"],
            "newest_run_history_at": run_history_span["newest"],
            "template_preview_count": preview_count,
            "template_decision_count": decision_count,
            "ai_generation_count": ai_generation_count,
        }

    # -- template suggestions (rule-engine output, informational only) -----

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
        cur = self._execute_write(
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
        return cur.lastrowid

    # -- template actions (raw button-press log) ----------------------------

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
        interaction_chat_id: str | int | None = None,
    ) -> int:
        cur = self._execute_write(
            """
            INSERT INTO template_actions (
                message_id, selected_template_id, selected_by, selected_at,
                callback_query_id, extraction_json, rendered_text, status, error,
                interaction_chat_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                None if interaction_chat_id is None else str(interaction_chat_id),
            ),
        )
        return cur.lastrowid

    def update_template_action_status(
        self, action_id: int, *, status: str, error: str | None = None
    ) -> None:
        self._execute_write(
            "UPDATE template_actions SET status = ?, error = ? WHERE action_id = ?",
            (status, error, action_id),
        )

    # -- template previews ---------------------------------------------------

    def insert_preview(
        self,
        *,
        message_id: int,
        selected_template_id: str,
        selected_by: int,
        extraction_method: str,
        extracted_slots_json: str,
        rendered_text: str | None,
        missing_slots_json: str,
        status: str,
        ai_generation_id: int | None = None,
        interaction_chat_id: str | int | None = None,
    ) -> int:
        now = datetime.now(tz=SEOUL_TZ).isoformat()
        cur = self._execute_write(
            """
            INSERT INTO template_previews (
                message_id, selected_template_id, selected_by, extraction_method,
                extracted_slots_json, rendered_text, missing_slots_json, status,
                created_at, updated_at, ai_generation_id, interaction_chat_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                selected_template_id,
                selected_by,
                extraction_method,
                extracted_slots_json,
                rendered_text,
                missing_slots_json,
                status,
                now,
                now,
                ai_generation_id,
                str(interaction_chat_id) if interaction_chat_id is not None else None,
            ),
        )
        return cur.lastrowid

    def get_preview(self, preview_id: int) -> TemplatePreview | None:
        cur = self._conn.execute(
            "SELECT * FROM template_previews WHERE preview_id = ?", (preview_id,)
        )
        row = cur.fetchone()
        if row is None:
            return None
        return TemplatePreview(
            preview_id=row["preview_id"],
            message_id=row["message_id"],
            selected_template_id=row["selected_template_id"],
            selected_by=row["selected_by"],
            extraction_method=row["extraction_method"],
            extracted_slots_json=row["extracted_slots_json"],
            rendered_text=row["rendered_text"],
            missing_slots_json=row["missing_slots_json"],
            status=row["status"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            ai_generation_id=row["ai_generation_id"],
            interaction_chat_id=row["interaction_chat_id"],
        )

    def update_preview_status(
        self,
        preview_id: int,
        *,
        status: str,
        rendered_text: str | None = None,
        extracted_slots_json: str | None = None,
        missing_slots_json: str | None = None,
        ai_generation_id: int | None = None,
    ) -> None:
        current = self.get_preview(preview_id)
        if current is None:
            return
        self._execute_write(
            """
            UPDATE template_previews
            SET status = ?, rendered_text = ?, extracted_slots_json = ?,
                missing_slots_json = ?, ai_generation_id = ?, updated_at = ?
            WHERE preview_id = ?
            """,
            (
                status,
                rendered_text if rendered_text is not None else current.rendered_text,
                extracted_slots_json
                if extracted_slots_json is not None
                else current.extracted_slots_json,
                missing_slots_json
                if missing_slots_json is not None
                else current.missing_slots_json,
                ai_generation_id if ai_generation_id is not None else current.ai_generation_id,
                datetime.now(tz=SEOUL_TZ).isoformat(),
                preview_id,
            ),
        )

    def supersede_active_previews(
        self, *, message_id: int, selected_by: int, interaction_chat_id: str | int
    ) -> None:
        """Mark any existing active preview (rule_preview/ai_preview) for the
        same message_id + selected_by + interaction_chat_id as superseded, so
        its confirm/cancel buttons stop being honored once a newer preview
        exists. Scoped by chat (v0.4.0) so Operator A never supersedes
        Operator B, and the same user in another chat cannot supersede this
        one. Confirmed, cancelled, failed, and already-superseded rows are
        left untouched."""
        now = datetime.now(tz=SEOUL_TZ).isoformat()
        self._execute_write(
            """
            UPDATE template_previews
            SET status = 'superseded', updated_at = ?
            WHERE message_id = ? AND selected_by = ? AND interaction_chat_id = ?
                AND status IN ('rule_preview', 'ai_preview')
            """,
            (now, message_id, selected_by, str(interaction_chat_id)),
        )

    # -- template decisions (authoritative ground truth) ---------------------

    def upsert_decision(
        self,
        *,
        message_id: int,
        preview_id: int,
        final_template_id: str,
        final_slots_json: str,
        final_rendered_text: str,
        generation_method: str,
        confirmed_by: int,
        interaction_chat_id: str | int,
        source_id_snapshot: str | None = None,
        sender_or_region_snapshot: str | None = None,
        sent_at_snapshot: str | None = None,
        original_body_snapshot: str | None = None,
    ) -> int:
        """Write the authoritative decision for one operator+chat, including an
        immutable snapshot of the source message at confirmation time
        (source_id/sender-region/sent_at/original_body) so the training pair
        survives the source `messages` row being deleted by retention cleanup.

        v0.4.0: the conflict key is (message_id, confirmed_by,
        interaction_chat_id) — reconfirming a different preview for the same
        message/operator/chat updates only that operator's row in place; a
        different operator (or the same operator in a different chat) creates
        an independent, coexisting decision. See docs/database_retention.md
        and docs/independent_operator_model.md."""
        now = datetime.now(tz=SEOUL_TZ).isoformat()
        chat = str(interaction_chat_id) if interaction_chat_id is not None else "legacy"
        cur = self._execute_write(
            """
            INSERT INTO template_decisions (
                message_id, preview_id, final_template_id, final_slots_json,
                final_rendered_text, generation_method, confirmed_by, confirmed_at,
                interaction_chat_id, source_id_snapshot, sender_or_region_snapshot,
                sent_at_snapshot, original_body_snapshot
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(message_id, confirmed_by, interaction_chat_id) DO UPDATE SET
                preview_id = excluded.preview_id,
                final_template_id = excluded.final_template_id,
                final_slots_json = excluded.final_slots_json,
                final_rendered_text = excluded.final_rendered_text,
                generation_method = excluded.generation_method,
                confirmed_at = excluded.confirmed_at,
                source_id_snapshot = excluded.source_id_snapshot,
                sender_or_region_snapshot = excluded.sender_or_region_snapshot,
                sent_at_snapshot = excluded.sent_at_snapshot,
                original_body_snapshot = excluded.original_body_snapshot
            """,
            (
                message_id,
                preview_id,
                final_template_id,
                final_slots_json,
                final_rendered_text,
                generation_method,
                confirmed_by,
                now,
                chat,
                source_id_snapshot,
                sender_or_region_snapshot,
                sent_at_snapshot,
                original_body_snapshot,
            ),
        )
        if cur.lastrowid:
            return cur.lastrowid
        row = self._conn.execute(
            "SELECT decision_id FROM template_decisions "
            "WHERE message_id = ? AND confirmed_by = ? AND interaction_chat_id = ?",
            (message_id, confirmed_by, chat),
        ).fetchone()
        return row["decision_id"]

    def get_decision_for_operator(
        self, message_id: int, confirmed_by: int, interaction_chat_id: str | int
    ) -> sqlite3.Row | None:
        """The single decision belonging to one operator+chat (v0.4.0)."""
        return self._conn.execute(
            "SELECT * FROM template_decisions "
            "WHERE message_id = ? AND confirmed_by = ? AND interaction_chat_id = ?",
            (message_id, confirmed_by, str(interaction_chat_id)),
        ).fetchone()

    def list_decisions_for_message(self, message_id: int) -> list[sqlite3.Row]:
        """Every operator's decision for a source message (may be several)."""
        return self._conn.execute(
            "SELECT * FROM template_decisions WHERE message_id = ? ORDER BY decision_id ASC",
            (message_id,),
        ).fetchall()

    def get_decision_by_message_id(self, message_id: int) -> sqlite3.Row | None:
        """Compat helper: the most recent decision for a message. Since v0.4.0
        a message may have one decision per operator+chat — prefer
        `get_decision_for_operator` / `list_decisions_for_message`."""
        cur = self._conn.execute(
            "SELECT * FROM template_decisions WHERE message_id = ? ORDER BY decision_id DESC LIMIT 1",
            (message_id,),
        )
        return cur.fetchone()

    # -- AI generations -------------------------------------------------------

    def insert_ai_generation(
        self,
        *,
        message_id: int,
        selected_template_id: str,
        requested_by: int,
        model: str,
        prompt_version: str,
        request_slots_json: str,
        interaction_chat_id: str | int | None = None,
    ) -> int:
        cur = self._execute_write(
            """
            INSERT INTO ai_generations (
                message_id, selected_template_id, requested_by, model, prompt_version,
                request_slots_json, status, created_at, interaction_chat_id
            ) VALUES (?, ?, ?, ?, ?, ?, 'requested', ?, ?)
            """,
            (
                message_id,
                selected_template_id,
                requested_by,
                model,
                prompt_version,
                request_slots_json,
                datetime.now(tz=SEOUL_TZ).isoformat(),
                None if interaction_chat_id is None else str(interaction_chat_id),
            ),
        )
        return cur.lastrowid

    def update_ai_generation(
        self,
        ai_generation_id: int,
        *,
        status: str,
        response_json: str | None = None,
        validated_slots_json: str | None = None,
        error: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        estimated_cost: float | None = None,
    ) -> None:
        self._execute_write(
            """
            UPDATE ai_generations
            SET status = ?, response_json = ?, validated_slots_json = ?, error = ?,
                input_tokens = ?, output_tokens = ?, estimated_cost = ?
            WHERE ai_generation_id = ?
            """,
            (
                status,
                response_json,
                validated_slots_json,
                error,
                input_tokens,
                output_tokens,
                estimated_cost,
                ai_generation_id,
            ),
        )

    # -- personal Telegram subscriptions (v0.4.0) ---------------------------
    #
    # Every authorized operator has one equal, independent subscription.
    # There is no primary/default/representative row — active subscriptions
    # are peers. Only a private chat may be active. See
    # docs/independent_operator_model.md.

    @staticmethod
    def _row_to_subscription(row: sqlite3.Row) -> Subscription:
        return Subscription(
            subscription_id=row["subscription_id"],
            user_id=row["user_id"],
            chat_id=row["chat_id"],
            chat_type=row["chat_type"],
            status=row["status"],
            registration_source=row["registration_source"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            last_seen_at=datetime.fromisoformat(row["last_seen_at"]),
        )

    def get_subscription_by_user(self, user_id: int) -> Subscription | None:
        row = self._conn.execute(
            "SELECT * FROM telegram_subscriptions WHERE user_id = ?", (user_id,)
        ).fetchone()
        return self._row_to_subscription(row) if row is not None else None

    def get_subscription_by_chat(self, chat_id: str | int) -> Subscription | None:
        row = self._conn.execute(
            "SELECT * FROM telegram_subscriptions WHERE chat_id = ?", (str(chat_id),)
        ).fetchone()
        return self._row_to_subscription(row) if row is not None else None

    def list_active_subscriptions(
        self, allowed_user_ids: tuple[int, ...] | list[int] | None = None
    ) -> list[Subscription]:
        """Active, private subscriptions filtered to the currently authorized
        users. Ordering (subscription_id ASC) is deterministic for tests only
        and carries no priority — every returned subscription is an equal
        peer. A user removed from TELEGRAM_ALLOWED_USER_IDS is excluded even
        if a stale active row remains."""
        rows = self._conn.execute(
            "SELECT * FROM telegram_subscriptions "
            "WHERE status = 'active' AND chat_type = 'private' "
            "ORDER BY subscription_id ASC"
        ).fetchall()
        subs = [self._row_to_subscription(r) for r in rows]
        if allowed_user_ids is not None:
            allowed = set(allowed_user_ids)
            subs = [s for s in subs if s.user_id in allowed]
        return subs

    def register_or_touch_subscription(
        self,
        *,
        user_id: int,
        chat_id: str | int,
        chat_type: str,
        registration_source: str = "interaction",
        activate_if_new: bool = True,
    ) -> Subscription:
        """Touch an existing subscription's last_seen_at without changing a
        muted/unsubscribed status (only /subscribe, /mute, /unmute may change
        status). A previously unknown authorized private user is created
        active (when activate_if_new)."""
        now = datetime.now(tz=SEOUL_TZ).isoformat()
        existing = self.get_subscription_by_user(user_id)
        if existing is None:
            status = "active" if (activate_if_new and chat_type == "private") else "unsubscribed"
            self._execute_write(
                "INSERT OR IGNORE INTO telegram_subscriptions "
                "(user_id, chat_id, chat_type, status, registration_source, "
                "created_at, updated_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, str(chat_id), chat_type, status, registration_source, now, now, now),
            )
            return self.get_subscription_by_user(user_id)
        # Existing row: only touch last_seen_at (and updated_at). Status is
        # preserved — a muted or unsubscribed user is never silently
        # reactivated by ordinary interaction.
        self._execute_write(
            "UPDATE telegram_subscriptions SET last_seen_at = ?, updated_at = ? WHERE user_id = ?",
            (now, now, user_id),
        )
        return self.get_subscription_by_user(user_id)

    def set_subscription_status(
        self, *, user_id: int, chat_id: str | int, status: str
    ) -> Subscription:
        """Explicit status change from /subscribe|/unsubscribe|/mute|/unmute.
        Binds the subscription to the current private chat_id."""
        now = datetime.now(tz=SEOUL_TZ).isoformat()
        existing = self.get_subscription_by_user(user_id)
        if existing is None:
            self._execute_write(
                "INSERT OR IGNORE INTO telegram_subscriptions "
                "(user_id, chat_id, chat_type, status, registration_source, "
                "created_at, updated_at, last_seen_at) VALUES (?, ?, 'private', ?, 'command', ?, ?, ?)",
                (user_id, str(chat_id), status, now, now, now),
            )
            return self.get_subscription_by_user(user_id)
        self._execute_write(
            "UPDATE telegram_subscriptions SET status = ?, chat_id = ?, chat_type = 'private', "
            "updated_at = ?, last_seen_at = ? WHERE user_id = ?",
            (status, str(chat_id), now, now, user_id),
        )
        return self.get_subscription_by_user(user_id)

    def subscription_status_for(self, user_id: int, chat_id: str | int | None = None) -> str:
        """Return active|muted|unsubscribed, or 'none' when the user has no row."""
        sub = self.get_subscription_by_user(user_id)
        return sub.status if sub is not None else "none"

    def seed_subscriptions_if_empty(
        self, *, allowed_user_ids: tuple[int, ...] | list[int], legacy_chat_id: str | None
    ) -> int:
        """One-time bootstrap when telegram_subscriptions is empty (v0.4.0
        migration). Seeds active subscriptions from distinct historical
        Preview owners (selected_by + interaction_chat_id) for authorized
        users on positive/private-looking chats, then optionally the legacy
        TELEGRAM_CHAT_ID if it is a positive/private chat safely matchable to
        exactly one allowed user. Never seeds negative group IDs and never
        guesses an owner. Returns the number of subscriptions created."""
        existing = self._conn.execute(
            "SELECT COUNT(*) AS c FROM telegram_subscriptions"
        ).fetchone()["c"]
        if existing:
            return 0

        allowed = set(allowed_user_ids)
        if not allowed:
            return 0

        now = datetime.now(tz=SEOUL_TZ).isoformat()
        created = 0
        seeded_users: set[int] = set()
        seeded_chats: set[str] = set()

        rows = self._conn.execute(
            "SELECT selected_by, interaction_chat_id, MAX(created_at) AS last_at "
            "FROM template_previews "
            "WHERE interaction_chat_id IS NOT NULL "
            "GROUP BY selected_by, interaction_chat_id "
            "ORDER BY last_at DESC"
        ).fetchall()
        for row in rows:
            user_id = row["selected_by"]
            chat_id = row["interaction_chat_id"]
            if user_id not in allowed or user_id in seeded_users:
                continue
            if not _looks_like_private_chat(chat_id) or chat_id in seeded_chats:
                continue
            self._execute_write(
                "INSERT OR IGNORE INTO telegram_subscriptions "
                "(user_id, chat_id, chat_type, status, registration_source, "
                "created_at, updated_at, last_seen_at) "
                "VALUES (?, ?, 'private', 'active', 'migrated_preview', ?, ?, ?)",
                (user_id, str(chat_id), now, now, now),
            )
            seeded_users.add(user_id)
            seeded_chats.add(str(chat_id))
            created += 1

        # Legacy TELEGRAM_CHAT_ID: only usable if it is a positive/private
        # chat, not already seeded, and there is exactly one still-unseeded
        # allowed user to attribute it to (otherwise the owner is a guess).
        if (
            legacy_chat_id
            and _looks_like_private_chat(legacy_chat_id)
            and str(legacy_chat_id) not in seeded_chats
        ):
            remaining = [uid for uid in allowed if uid not in seeded_users]
            if len(remaining) == 1:
                self._execute_write(
                    "INSERT OR IGNORE INTO telegram_subscriptions "
                    "(user_id, chat_id, chat_type, status, registration_source, "
                    "created_at, updated_at, last_seen_at) "
                    "VALUES (?, ?, 'private', 'active', 'legacy_env', ?, ?, ?)",
                    (remaining[0], str(legacy_chat_id), now, now, now),
                )
                created += 1

        return created

    def subscription_counts(self) -> dict[str, int]:
        """Aggregate subscription counts by status (no IDs) for safe reports."""
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS c FROM telegram_subscriptions GROUP BY status"
        ).fetchall()
        return {row["status"]: row["c"] for row in rows}

    # -- per-recipient automatic delivery (v0.4.0) --------------------------
    #
    # telegram_deliveries is the source of truth for personal automatic
    # delivery/retry. messages.telegram_status is only a derived aggregate
    # kept for backward compatibility (see docs/independent_operator_model.md).

    def create_delivery_if_missing(
        self, *, message_id: int, subscription: Subscription
    ) -> int | None:
        """Create a pending delivery row for (message_id, subscription) unless
        one already exists (dedup is UNIQUE(message_id, subscription_id) only —
        never across users). Returns the new delivery_id, or None if it
        already existed."""
        now = datetime.now(tz=SEOUL_TZ).isoformat()
        cur = self._execute_write(
            "INSERT OR IGNORE INTO telegram_deliveries "
            "(message_id, subscription_id, user_id_snapshot, chat_id_snapshot, "
            "status, attempts, created_at) VALUES (?, ?, ?, ?, 'pending', 0, ?)",
            (
                message_id,
                subscription.subscription_id,
                subscription.user_id,
                subscription.chat_id,
                now,
            ),
        )
        # rowcount (not lastrowid) is the reliable "was a row inserted" signal:
        # after an ignored INSERT OR IGNORE, lastrowid retains the previous
        # insert's rowid, so it cannot distinguish a real insert from a dedup.
        return cur.lastrowid if cur.rowcount > 0 else None

    def list_retryable_deliveries(
        self, allowed_user_ids: tuple[int, ...] | list[int]
    ) -> list[sqlite3.Row]:
        """pending/failed deliveries whose subscription is still active+private,
        whose source message still exists (non-baseline), AND whose owning user
        is currently present in TELEGRAM_ALLOWED_USER_IDS. Retry logic uses this
        — never messages.telegram_status.

        A user removed from the allowed list is never retried again: the
        historical delivery row is kept for audit, just excluded from the retry
        set (mirrors list_active_subscriptions' allowed-user filter so a new
        alert and a retry authorize identically). An empty allowed list safely
        yields no rows — the IN (...) clause is only built from a
        placeholder-per-id, never from an empty list (which would be invalid
        SQL), and there is no TELEGRAM_CHAT_ID fallback."""
        allowed = list(dict.fromkeys(allowed_user_ids))
        if not allowed:
            return []
        placeholders = ",".join("?" for _ in allowed)
        return self._conn.execute(
            f"""
            SELECT d.*, s.chat_id AS sub_chat_id, s.user_id AS sub_user_id,
                   s.status AS sub_status, s.chat_type AS sub_chat_type
            FROM telegram_deliveries d
            JOIN telegram_subscriptions s ON s.subscription_id = d.subscription_id
            JOIN messages m ON m.internal_id = d.message_id
            WHERE d.status IN ('pending', 'failed')
              AND s.status = 'active'
              AND s.chat_type = 'private'
              AND m.is_baseline = 0
              AND s.user_id IN ({placeholders})
            ORDER BY d.delivery_id ASC
            """,
            tuple(allowed),
        ).fetchall()

    def mark_delivery_sent(self, delivery_id: int, *, telegram_message_id: str | None) -> None:
        now = datetime.now(tz=SEOUL_TZ).isoformat()
        self._execute_write(
            "UPDATE telegram_deliveries SET status = 'sent', telegram_message_id = ?, "
            "attempts = attempts + 1, last_attempt_at = ?, sent_at = ?, error = NULL "
            "WHERE delivery_id = ?",
            (telegram_message_id, now, now, delivery_id),
        )

    def mark_delivery_failed(self, delivery_id: int, *, error: str | None) -> None:
        now = datetime.now(tz=SEOUL_TZ).isoformat()
        self._execute_write(
            "UPDATE telegram_deliveries SET status = 'failed', attempts = attempts + 1, "
            "last_attempt_at = ?, error = ? WHERE delivery_id = ?",
            (now, (error or "")[:500], delivery_id),
        )

    def get_deliveries_for_message(self, message_id: int) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM telegram_deliveries WHERE message_id = ? ORDER BY delivery_id ASC",
            (message_id,),
        ).fetchall()

    def get_latest_delivery_for_user(
        self, user_id: int, chat_id: str | int | None = None
    ) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM telegram_deliveries WHERE user_id_snapshot = ? "
            "ORDER BY COALESCE(sent_at, last_attempt_at, created_at) DESC, delivery_id DESC LIMIT 1",
            (user_id,),
        ).fetchone()

    def update_message_aggregate_status(self, message_id: int) -> None:
        """Derive messages.telegram_status from its per-recipient deliveries.
        Compat aggregate only — retries use telegram_deliveries, and
        messages.telegram_message_id holds only the first successful id."""
        rows = self.get_deliveries_for_message(message_id)
        if not rows:
            self.update_telegram_result(
                message_id, status=TelegramStatus.TELEGRAM_PENDING, message_id=None
            )
            return
        statuses = {r["status"] for r in rows}
        if statuses == {"sent"}:
            status = TelegramStatus.TELEGRAM_SENT
        elif "failed" in statuses:
            status = TelegramStatus.TELEGRAM_FAILED
        else:
            status = TelegramStatus.TELEGRAM_PENDING
        sent_ids = [
            r["telegram_message_id"]
            for r in rows
            if r["status"] == "sent" and r["telegram_message_id"]
        ]
        self.update_telegram_result(
            message_id, status=status, message_id=sent_ids[0] if sent_ids else None
        )

    # -- duplicate-callback guard (generalized across all callback types) --

    def has_processed_callback(self, callback_query_id: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM processed_callback_queries WHERE callback_query_id = ? LIMIT 1",
            (callback_query_id,),
        )
        return cur.fetchone() is not None

    def mark_callback_processed(self, callback_query_id: str) -> None:
        self._execute_write(
            "INSERT OR IGNORE INTO processed_callback_queries (callback_query_id, processed_at) "
            "VALUES (?, ?)",
            (callback_query_id, datetime.now(tz=SEOUL_TZ).isoformat()),
        )

    # -- Telegram getUpdates offset (persisted so a restart never replays
    # already-handled updates) --------------------------------------------

    def get_telegram_update_offset(self) -> int:
        cur = self._conn.execute("SELECT telegram_update_offset FROM system_state WHERE id = 1")
        return cur.fetchone()["telegram_update_offset"]

    def set_telegram_update_offset(self, offset: int) -> None:
        self._execute_write(
            "UPDATE system_state SET telegram_update_offset = ? WHERE id = 1", (offset,)
        )


@contextmanager
def open_database(path: Path) -> Iterator[Database]:
    db = Database(path)
    try:
        yield db
    finally:
        db.close()
