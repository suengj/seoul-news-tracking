from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.database import Database
from app.models import TelegramStatus

SEOUL_TZ = ZoneInfo("Asia/Seoul")


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "test.db")
    yield database
    database.close()


def test_initial_baseline_behavior(db, make_record):
    assert db.is_empty
    r1 = make_record(source_id="DS1")
    r2 = make_record(source_id="DS2", sent_at=datetime(2026, 7, 13, 10, 0, tzinfo=SEOUL_TZ))
    db.insert(r1, is_baseline=True)
    db.insert(r2, is_baseline=True)
    assert not db.is_empty
    assert db.known_source_ids() == {"DS1", "DS2"}


def test_stable_source_id_dedup(db, make_record):
    r1 = make_record(source_id="DS1")
    db.insert(r1)
    r1_again = make_record(source_id="DS1", body="different body but same id")
    assert db.is_known(r1_again)


def test_hash_fallback_dedup_without_stable_id_change(db, make_record):
    # Same sender/sent_at/body (thus same raw_hash) but source id differs —
    # still recognized as a duplicate via the hash fallback.
    r1 = make_record(source_id="DS1", body="identical content")
    db.insert(r1)
    r2 = make_record(source_id="DS-DIFFERENT", body="identical content")
    assert db.is_known(r2)


def test_duplicate_prevention_across_polls(db, make_record):
    r1 = make_record(source_id="DS1")
    db.insert(r1)
    # Simulate re-polling: same record appears again.
    r1_repeat = make_record(source_id="DS1")
    assert db.is_known(r1_repeat)


def test_overlapping_five_record_windows(db, make_record):
    # First poll returns records A,B,C,D,E; second poll (with "더보기"-style
    # overlap) returns C,D,E,F,G. Only F and G should be new.
    first_window = [make_record(source_id=f"DS{i}") for i in range(1, 6)]
    for r in first_window:
        db.insert(r)

    second_window_ids = ["DS3", "DS4", "DS5", "DS6", "DS7"]
    new_ids = [sid for sid in second_window_ids if sid not in db.known_source_ids()]
    assert new_ids == ["DS6", "DS7"]


def test_changed_record_ordering_does_not_affect_dedup(db, make_record):
    ordered = [make_record(source_id=f"DS{i}") for i in (1, 2, 3)]
    for r in ordered:
        db.insert(r)

    reordered_ids = ["DS3", "DS1", "DS2"]
    assert all(sid in db.known_source_ids() for sid in reordered_ids)


def test_pending_retry_records_excludes_sent_and_baseline(db, make_record):
    baseline = make_record(source_id="DS-BASE")
    db.insert(baseline, is_baseline=True)

    sent = make_record(source_id="DS-SENT")
    db.insert(sent)
    db.update_telegram_result(sent.internal_id, status=TelegramStatus.TELEGRAM_SENT, message_id="1")

    failed = make_record(source_id="DS-FAILED")
    db.insert(failed)
    db.update_telegram_result(failed.internal_id, status=TelegramStatus.TELEGRAM_FAILED, message_id=None)

    pending_ids = {r.source_id for r in db.pending_retry_records()}
    assert pending_ids == {"DS-FAILED"}


def test_run_history_tracks_counts(db):
    run_id = db.start_run("poll_once")
    db.finish_run(
        run_id,
        status="ok",
        fetched_count=5,
        new_count=2,
        duplicate_count=3,
        sent_count=2,
        failed_count=0,
    )
    cur = db._conn.execute("SELECT * FROM run_history WHERE run_id = ?", (run_id,))
    row = cur.fetchone()
    assert row["status"] == "ok"
    assert row["new_count"] == 2
    assert row["duplicate_count"] == 3
