from __future__ import annotations

from pathlib import Path

import pytest

from app.history_database import (
    DuplicateRecordError,
    HistoryDatabase,
    RUN_STATUS_COMPLETED,
    RUN_STATUS_PAUSED,
)
from app.history_models import HistoricalRawRecord


def _make_record(source_id="1", body="body", sender="관리자"):
    return HistoricalRawRecord(
        source="test",
        source_id=source_id,
        sent_at_raw="2026/07/13 10:00:00",
        sent_at=None,
        sender_raw=sender,
        region_raw="구",
        title_raw="title",
        body_raw=body,
        list_url="https://example.test/list",
        detail_url=f"https://example.test/detail?sn={source_id}",
        raw_payload={"k": "v"},
        source_page=1,
        source_position=1,
    )


@pytest.fixture
def db(tmp_path: Path) -> HistoryDatabase:
    database = HistoryDatabase(tmp_path / "history.db")
    yield database
    database.close()


def test_insert_and_unique_count(db):
    db.insert_record(_make_record(source_id="1"))
    db.insert_record(_make_record(source_id="2"))
    assert db.unique_count() == 2


def test_duplicate_source_id_rejected(db):
    db.insert_record(_make_record(source_id="1", body="original"))
    with pytest.raises(DuplicateRecordError):
        db.insert_record(_make_record(source_id="1", body="different body, same id"))
    assert db.unique_count() == 1


def test_duplicate_raw_hash_rejected_without_source_id(db):
    record_a = _make_record(source_id="1")
    record_a.source_id = None
    record_a.raw_hash = "identical-hash"
    record_b = _make_record(source_id="2")
    record_b.source_id = None
    record_b.raw_hash = "identical-hash"

    db.insert_record(record_a)
    with pytest.raises(DuplicateRecordError):
        db.insert_record(record_b)
    assert db.unique_count() == 1


def test_is_known_source_id(db):
    assert db.is_known_source_id("1") is False
    db.insert_record(_make_record(source_id="1"))
    assert db.is_known_source_id("1") is True


def test_crawl_run_lifecycle(db):
    assert db.find_active_run() is None
    run_id = db.start_run(target_count=10)
    active = db.find_active_run()
    assert active["run_id"] == run_id
    assert active["status"] == "running"

    db.update_run_progress(
        run_id,
        current_page=2,
        pages_processed=1,
        fetched_count=10,
        inserted_count=5,
        duplicate_count=0,
        malformed_count=0,
        error_count=0,
        retry_count=0,
        status=RUN_STATUS_PAUSED,
    )
    paused = db.find_active_run()
    assert paused["status"] == RUN_STATUS_PAUSED
    assert paused["current_page"] == 2

    db.finish_run(run_id, status=RUN_STATUS_COMPLETED)
    assert db.find_active_run() is None
    assert db.latest_run()["status"] == RUN_STATUS_COMPLETED


def test_validation_stats_reports_missing_fields(db):
    db.insert_record(_make_record(source_id="1", sender="관리자"))
    record_missing_sender = _make_record(source_id="2")
    record_missing_sender.sender_raw = None
    db.insert_record(record_missing_sender)

    stats = db.validation_stats()
    assert stats["total"] == 2
    assert stats["missing_sender"] == 1
    assert stats["duplicate_source_id_groups"] == 0
    assert stats["duplicate_raw_hash_groups"] == 0


def test_migration_is_idempotent(tmp_path: Path):
    path = tmp_path / "history.db"
    db1 = HistoryDatabase(path)
    db1.insert_record(_make_record(source_id="1"))
    db1.close()

    db2 = HistoryDatabase(path)  # re-running CREATE TABLE IF NOT EXISTS must not fail or wipe data
    assert db2.unique_count() == 1
    db2.close()
