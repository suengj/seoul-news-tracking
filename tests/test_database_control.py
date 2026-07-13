from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.database import Database

SEOUL_TZ = ZoneInfo("Asia/Seoul")


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "control.db")
    yield database
    database.close()


# -- get_latest_record ---------------------------------------------------


def test_get_latest_record_empty_db_returns_none(db):
    assert db.get_latest_record() is None


def test_get_latest_record_orders_by_sent_at_desc(db, make_record):
    older = make_record(source_id="OLD", sent_at=datetime(2026, 7, 1, tzinfo=SEOUL_TZ))
    newer = make_record(source_id="NEW", sent_at=datetime(2026, 7, 10, tzinfo=SEOUL_TZ))
    db.insert(older)
    db.insert(newer)
    latest = db.get_latest_record()
    assert latest.source_id == "NEW"


def test_records_read_back_from_db_retain_seoul_zone_name(db, make_record):
    """Regression test: datetime.fromisoformat() on a stored "+09:00" string
    reconstructs a fixed-offset tzinfo (strftime('%Z') -> 'UTC+09:00'), not
    the original ZoneInfo('Asia/Seoul') ('%Z' -> 'KST'). A live /latest
    reply once showed 'UTC+09:00' instead of 'KST' because of this. Rows
    read back from the DB must carry the real zone name."""
    db.insert(make_record(source_id="TZ1", sent_at=datetime(2026, 7, 13, 9, 0, 0, tzinfo=SEOUL_TZ)))
    record = db.get_latest_record()
    assert record.sent_at.strftime("%Z") == "KST"
    assert record.detected_at.strftime("%Z") == "KST"


def test_get_latest_record_tiebreaks_on_internal_id_desc(db, make_record):
    # Same sent_at, inserted in order; internal_id DESC must break the tie.
    same_time = datetime(2026, 7, 10, 9, 0, 0, tzinfo=SEOUL_TZ)
    first = make_record(source_id="FIRST", sent_at=same_time, body="first body")
    second = make_record(source_id="SECOND", sent_at=same_time, body="second body")
    db.insert(first)
    db.insert(second)
    latest = db.get_latest_record()
    assert latest.source_id == "SECOND"


# -- pause / resume idempotency and persistence --------------------------


def test_default_polling_state_is_enabled(db):
    assert db.is_polling_enabled() is True


def test_pause_is_idempotent(db):
    assert db.pause_polling(actor_user_id=111) is True
    assert db.pause_polling(actor_user_id=111) is False
    assert db.pause_polling(actor_user_id=222) is False
    assert db.is_polling_enabled() is False


def test_resume_is_idempotent(db):
    db.pause_polling(actor_user_id=111)
    assert db.resume_polling(actor_user_id=222) is True
    assert db.resume_polling(actor_user_id=222) is False
    assert db.is_polling_enabled() is True


def test_pause_state_persists_after_db_reopen(tmp_path):
    path = tmp_path / "persist.db"
    db1 = Database(path)
    db1.pause_polling(actor_user_id=555)
    db1.close()

    db2 = Database(path)
    state = db2.get_system_state()
    assert state.polling_enabled is False
    assert state.paused_by == 555
    assert state.paused_at is not None
    db2.close()


def test_pause_and_resume_store_actor_user_id_not_display_name(db):
    db.pause_polling(actor_user_id=42)
    state = db.get_system_state()
    assert state.paused_by == 42
    db.resume_polling(actor_user_id=43)
    state = db.get_system_state()
    assert state.resumed_by == 43


# -- WAL / concurrency -----------------------------------------------------


def test_wal_journal_mode_configured(db):
    mode = db._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_busy_timeout_configured(db):
    timeout = db._conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert timeout == 5000


def test_foreign_keys_configured(db):
    value = db._conn.execute("PRAGMA foreign_keys").fetchone()[0]
    assert value == 1


def test_concurrent_read_write_from_two_connections(tmp_path, make_record):
    """Two independent connections to the same WAL-mode file (modeling the
    poller and Telegram bot as separate OS processes, each with its own
    single connection) can interleave writes and reads without locking
    errors or stale reads."""
    path = tmp_path / "concurrent.db"
    writer = Database(path)
    reader = Database(path)

    writer.insert(make_record(source_id="C1"))
    # A second connection should immediately see committed data under WAL.
    assert "C1" in reader.known_source_ids()

    for i in range(20):
        writer.insert(make_record(source_id=f"W{i}", body=f"body {i}"))
        # Interleaved reads from the other connection must see committed
        # writes and never raise "database is locked".
        ids = reader.known_source_ids()
        assert f"W{i}" in ids

    writer.close()
    reader.close()


class _FlakyConnProxy:
    """Wraps a real sqlite3.Connection, failing the first N INSERT executes
    with 'database is locked' to exercise Database._execute_write's retry."""

    def __init__(self, real_conn, fail_times: int):
        self._real = real_conn
        self._fail_times = fail_times
        self.attempts = 0

    def execute(self, sql, params=()):
        if sql.strip().upper().startswith("INSERT") and self.attempts < self._fail_times:
            self.attempts += 1
            raise sqlite3.OperationalError("database is locked")
        return self._real.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_write_lock_retry_recovers_from_transient_lock(tmp_path, make_record, monkeypatch):
    """_execute_write retries a bounded number of times on 'database is
    locked' instead of failing on the first contention."""
    db = Database(tmp_path / "retry.db")
    proxy = _FlakyConnProxy(db._conn, fail_times=2)
    monkeypatch.setattr(db, "_conn", proxy)

    db.insert(make_record(source_id="RETRY1"))
    assert proxy.attempts == 2
    assert "RETRY1" in db.known_source_ids()
    db.close()


def test_write_lock_gives_up_after_bounded_attempts(tmp_path, make_record, monkeypatch):
    from app.database import DatabaseLockedError

    db = Database(tmp_path / "retry_fail.db")
    proxy = _FlakyConnProxy(db._conn, fail_times=100)  # never succeeds
    monkeypatch.setattr(db, "_conn", proxy)

    with pytest.raises(DatabaseLockedError):
        db.insert(make_record(source_id="RETRY2"))
    # Bounded: not an unbounded/infinite retry loop.
    assert proxy.attempts == 3
    db.close()


# -- retention: message cleanup -------------------------------------------


@pytest.mark.parametrize("retention_days", [30, 90])
def test_message_retention_boundary(db, make_record, retention_days):
    now = datetime(2026, 12, 1, tzinfo=SEOUL_TZ)
    just_inside = now - timedelta(days=retention_days - 1)
    just_outside = now - timedelta(days=retention_days + 1)

    db.insert(make_record(source_id="KEEP", sent_at=just_inside, body="keep me"))
    db.insert(make_record(source_id="EXPIRE", sent_at=just_outside, body="expire me"))

    counts = db.cleanup_execute(
        message_retention_days=retention_days,
        run_history_retention_days=14,
        tombstone_retention_days=365,
        now=now,
    )
    assert counts.message_rows_eligible == 1
    assert db.known_source_ids() == {"KEEP"}


def test_cleanup_preview_does_not_modify_db(db, make_record):
    now = datetime(2026, 12, 1, tzinfo=SEOUL_TZ)
    old = now - timedelta(days=200)
    db.insert(make_record(source_id="OLD", sent_at=old))

    counts = db.cleanup_preview(
        message_retention_days=90,
        run_history_retention_days=14,
        tombstone_retention_days=365,
        now=now,
    )
    assert counts.message_rows_eligible == 1
    assert db.known_source_ids() == {"OLD"}  # untouched


def test_cleanup_creates_tombstone_preventing_renotification(db, make_record):
    now = datetime(2026, 12, 1, tzinfo=SEOUL_TZ)
    old_record = make_record(
        source_id="EXPIRED1", sent_at=now - timedelta(days=200), body="old news"
    )
    db.insert(old_record)

    db.cleanup_execute(
        message_retention_days=90,
        run_history_retention_days=14,
        tombstone_retention_days=365,
        now=now,
    )
    assert db.is_empty

    # If the same record ever reappeared in a poll window, it must still be
    # recognized as already-seen via the tombstone (not re-notified).
    reappeared = make_record(
        source_id="EXPIRED1", sent_at=now - timedelta(days=200), body="old news"
    )
    assert db.is_known(reappeared)


def test_tombstone_does_not_store_full_message_body(db, make_record):
    now = datetime(2026, 12, 1, tzinfo=SEOUL_TZ)
    db.insert(
        make_record(
            source_id="SECRET1", sent_at=now - timedelta(days=200), body="sensitive body text"
        )
    )
    db.cleanup_execute(
        message_retention_days=90,
        run_history_retention_days=14,
        tombstone_retention_days=365,
        now=now,
    )
    cur = db._conn.execute("SELECT * FROM tombstones")
    columns = [d[0] for d in cur.description]
    assert set(columns) == {"source_id", "raw_hash", "expired_at"}
    row = cur.fetchone()
    for value in row:
        assert "sensitive body text" not in str(value)


def test_tombstones_expire_after_their_own_retention(db, make_record):
    now = datetime(2026, 12, 1, tzinfo=SEOUL_TZ)
    db.insert(make_record(source_id="X1", sent_at=now - timedelta(days=200)))
    db.cleanup_execute(
        message_retention_days=90,
        run_history_retention_days=14,
        tombstone_retention_days=365,
        now=now,
    )
    tombstone_count = db._conn.execute("SELECT COUNT(*) AS c FROM tombstones").fetchone()["c"]
    assert tombstone_count == 1

    much_later = now + timedelta(days=400)
    db.cleanup_execute(
        message_retention_days=90,
        run_history_retention_days=14,
        tombstone_retention_days=365,
        now=much_later,
    )
    tombstone_count = db._conn.execute("SELECT COUNT(*) AS c FROM tombstones").fetchone()["c"]
    assert tombstone_count == 0


# -- retention: run-history -------------------------------------------------


def test_run_history_retention_deletes_old_rows(db):
    now = datetime(2026, 12, 1, tzinfo=SEOUL_TZ)
    old_started = (now - timedelta(days=20)).isoformat()
    db._conn.execute(
        "INSERT INTO run_history (run_type, started_at, status) VALUES ('poll_once', ?, 'ok')",
        (old_started,),
    )
    db._conn.commit()

    counts = db.cleanup_execute(
        message_retention_days=90,
        run_history_retention_days=14,
        tombstone_retention_days=365,
        now=now,
    )
    assert counts.run_history_rows_eligible == 1
    assert db.run_history_count() == 0


def test_successful_noop_run_suppressed_by_default(db):
    run_id = db.start_run("poll_once")
    db.finish_run(
        run_id,
        status="ok",
        fetched_count=5,
        new_count=0,
        duplicate_count=5,
        sent_count=0,
        failed_count=0,
        store_successful_noop_runs=False,
    )
    assert db.run_history_count() == 0


def test_successful_noop_run_kept_when_configured(db):
    run_id = db.start_run("poll_once")
    db.finish_run(
        run_id,
        status="ok",
        fetched_count=5,
        new_count=0,
        duplicate_count=5,
        sent_count=0,
        failed_count=0,
        store_successful_noop_runs=True,
    )
    assert db.run_history_count() == 1


def test_failed_run_always_preserved_even_with_suppression_enabled(db):
    run_id = db.start_run("poll_once")
    db.finish_run(
        run_id,
        status="failed",
        detail="collector error",
        store_successful_noop_runs=False,
    )
    assert db.run_history_count() == 1


def test_run_with_new_messages_always_preserved(db):
    run_id = db.start_run("poll_once")
    db.finish_run(
        run_id,
        status="ok",
        fetched_count=5,
        new_count=2,
        duplicate_count=3,
        sent_count=2,
        failed_count=0,
        store_successful_noop_runs=False,
    )
    assert db.run_history_count() == 1


def test_run_with_telegram_failures_always_preserved(db):
    run_id = db.start_run("poll_once")
    db.finish_run(
        run_id,
        status="ok",
        fetched_count=1,
        new_count=1,
        duplicate_count=0,
        sent_count=0,
        failed_count=1,
        store_successful_noop_runs=False,
    )
    assert db.run_history_count() == 1


def test_control_events_always_logged(db):
    db.pause_polling(actor_user_id=1)
    db.record_control_event("pause", actor_user_id=1, detail="via telegram")
    db.resume_polling(actor_user_id=1)
    db.record_control_event("resume", actor_user_id=1, detail="via telegram")
    assert db.run_history_count() == 2


# -- should_run_cleanup -----------------------------------------------------


def test_should_run_cleanup_true_when_never_run(db):
    assert db.should_run_cleanup(cleanup_interval_hours=24) is True


def test_should_run_cleanup_false_within_interval(db):
    now = datetime(2026, 12, 1, 12, 0, 0, tzinfo=SEOUL_TZ)
    db.record_cleanup_run(now=now)
    soon_after = now + timedelta(hours=1)
    assert db.should_run_cleanup(cleanup_interval_hours=24, now=soon_after) is False


def test_should_run_cleanup_true_after_interval_elapsed(db):
    now = datetime(2026, 12, 1, 12, 0, 0, tzinfo=SEOUL_TZ)
    db.record_cleanup_run(now=now)
    much_later = now + timedelta(hours=25)
    assert db.should_run_cleanup(cleanup_interval_hours=24, now=much_later) is True
