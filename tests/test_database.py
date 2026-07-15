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
    db.update_telegram_result(
        failed.internal_id, status=TelegramStatus.TELEGRAM_FAILED, message_id=None
    )

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


# --- Service v1: template previews / decisions / AI generations --------------


def test_insert_and_get_preview_roundtrip(db, make_record):
    record = make_record(source_id="DS1")
    db.insert(record)
    preview_id = db.insert_preview(
        message_id=record.internal_id,
        selected_template_id="HEAVY_RAIN_CLEARED",
        selected_by=111,
        extraction_method="rule",
        extracted_slots_json="{}",
        rendered_text="렌더된 문안",
        missing_slots_json="[]",
        status="rule_preview",
    )
    preview = db.get_preview(preview_id)
    assert preview.message_id == record.internal_id
    assert preview.status == "rule_preview"
    assert preview.rendered_text == "렌더된 문안"


def test_update_preview_status_updates_fields(db, make_record):
    record = make_record(source_id="DS1")
    db.insert(record)
    preview_id = db.insert_preview(
        message_id=record.internal_id,
        selected_template_id="HEAVY_RAIN_CLEARED",
        selected_by=111,
        extraction_method="rule",
        extracted_slots_json="{}",
        rendered_text=None,
        missing_slots_json='["지역"]',
        status="rule_preview",
    )
    db.update_preview_status(preview_id, status="cancelled")
    preview = db.get_preview(preview_id)
    assert preview.status == "cancelled"
    # Fields not passed to update_preview_status are preserved.
    assert preview.missing_slots_json == '["지역"]'


def test_upsert_decision_creates_then_updates_in_place(db, make_record):
    # Same operator + same chat reconfirming a different preview updates the
    # SAME decision row in place (v0.4.0 conflict key includes confirmed_by
    # + interaction_chat_id).
    record = make_record(source_id="DS1")
    db.insert(record)
    decision_id_1 = db.upsert_decision(
        message_id=record.internal_id,
        preview_id=1,
        final_template_id="HEAVY_RAIN_CLEARED",
        final_slots_json="{}",
        final_rendered_text="첫 번째 문안",
        generation_method="rule",
        confirmed_by=111,
        interaction_chat_id="100",
    )
    decision_id_2 = db.upsert_decision(
        message_id=record.internal_id,
        preview_id=2,
        final_template_id="FLOOD_ADVISORY_ISSUED",
        final_slots_json="{}",
        final_rendered_text="두 번째 문안",
        generation_method="ai",
        confirmed_by=111,
        interaction_chat_id="100",
    )
    assert decision_id_1 == decision_id_2  # same row, updated in place
    assert db._conn.execute("SELECT COUNT(*) AS c FROM template_decisions").fetchone()["c"] == 1
    row = db.get_decision_for_operator(record.internal_id, 111, "100")
    assert row["final_template_id"] == "FLOOD_ADVISORY_ISSUED"
    assert row["generation_method"] == "ai"
    assert row["confirmed_by"] == 111


def test_upsert_decision_is_independent_per_operator_and_chat(db, make_record):
    # Two different operators confirming the same source message create two
    # independent, coexisting decisions — one never overwrites the other.
    record = make_record(source_id="DS1")
    db.insert(record)
    id_a = db.upsert_decision(
        message_id=record.internal_id,
        preview_id=1,
        final_template_id="HEAVY_RAIN_CLEARED",
        final_slots_json="{}",
        final_rendered_text="A안",
        generation_method="rule",
        confirmed_by=111,
        interaction_chat_id="100",
    )
    id_b = db.upsert_decision(
        message_id=record.internal_id,
        preview_id=2,
        final_template_id="FLOOD_ADVISORY_ISSUED",
        final_slots_json="{}",
        final_rendered_text="B안",
        generation_method="ai",
        confirmed_by=222,
        interaction_chat_id="200",
    )
    assert id_a != id_b
    assert db._conn.execute("SELECT COUNT(*) AS c FROM template_decisions").fetchone()["c"] == 2
    assert len(db.list_decisions_for_message(record.internal_id)) == 2
    assert db.get_decision_for_operator(record.internal_id, 111, "100")["final_template_id"] == (
        "HEAVY_RAIN_CLEARED"
    )
    assert db.get_decision_for_operator(record.internal_id, 222, "200")["final_template_id"] == (
        "FLOOD_ADVISORY_ISSUED"
    )


def test_ai_generation_insert_and_update(db, make_record):
    record = make_record(source_id="DS1")
    db.insert(record)
    gen_id = db.insert_ai_generation(
        message_id=record.internal_id,
        selected_template_id="FLOOD_ADVISORY_ISSUED",
        requested_by=111,
        model="gpt-5-mini",
        prompt_version="v1",
        request_slots_json="{}",
    )
    row = db._conn.execute(
        "SELECT * FROM ai_generations WHERE ai_generation_id = ?", (gen_id,)
    ).fetchone()
    assert row["status"] == "requested"

    db.update_ai_generation(gen_id, status="succeeded", input_tokens=10, output_tokens=20)
    row = db._conn.execute(
        "SELECT * FROM ai_generations WHERE ai_generation_id = ?", (gen_id,)
    ).fetchone()
    assert row["status"] == "succeeded"
    assert row["input_tokens"] == 10


def test_processed_callback_queries_guard(db):
    assert db.has_processed_callback("cbq-1") is False
    db.mark_callback_processed("cbq-1")
    assert db.has_processed_callback("cbq-1") is True
    # Idempotent: marking twice must not raise.
    db.mark_callback_processed("cbq-1")


def test_upsert_decision_stores_source_snapshots(db, make_record):
    record = make_record(source_id="DS-SNAP", body="원문 전체 내용", sender="종로구")
    db.insert(record)
    db.upsert_decision(
        message_id=record.internal_id,
        preview_id=1,
        final_template_id="HEAVY_RAIN_CLEARED",
        final_slots_json="{}",
        final_rendered_text="문안",
        generation_method="rule",
        confirmed_by=111,
        interaction_chat_id="100",
        source_id_snapshot=record.source_id,
        sender_or_region_snapshot=record.sender_or_region,
        sent_at_snapshot=record.sent_at.isoformat(),
        original_body_snapshot=record.original_body,
    )
    row = db.get_decision_by_message_id(record.internal_id)
    assert row["source_id_snapshot"] == "DS-SNAP"
    assert row["sender_or_region_snapshot"] == "종로구"
    assert row["original_body_snapshot"] == "원문 전체 내용"


def test_reconfirm_updates_snapshots_in_place(db, make_record):
    record = make_record(source_id="DS-SNAP2", body="첫 원문", sender="종로구")
    db.insert(record)
    db.upsert_decision(
        message_id=record.internal_id,
        preview_id=1,
        final_template_id="HEAVY_RAIN_CLEARED",
        final_slots_json="{}",
        final_rendered_text="문안1",
        generation_method="rule",
        confirmed_by=111,
        interaction_chat_id="100",
        source_id_snapshot=record.source_id,
        sender_or_region_snapshot=record.sender_or_region,
        sent_at_snapshot=record.sent_at.isoformat(),
        original_body_snapshot=record.original_body,
    )
    # Reconfirm via a different preview for the same message (e.g. a
    # different template picked afterward) updates the same row + snapshots.
    db.upsert_decision(
        message_id=record.internal_id,
        preview_id=2,
        final_template_id="FLOOD_ADVISORY_ISSUED",
        final_slots_json="{}",
        final_rendered_text="문안2",
        generation_method="rule",
        confirmed_by=111,
        interaction_chat_id="100",
        source_id_snapshot=record.source_id,
        sender_or_region_snapshot=record.sender_or_region,
        sent_at_snapshot=record.sent_at.isoformat(),
        original_body_snapshot="갱신된 원문",
    )
    assert db._conn.execute("SELECT COUNT(*) AS c FROM template_decisions").fetchone()["c"] == 1
    row = db.get_decision_by_message_id(record.internal_id)
    assert row["final_template_id"] == "FLOOD_ADVISORY_ISSUED"
    assert row["original_body_snapshot"] == "갱신된 원문"


def test_snapshot_columns_migrate_onto_pre_change_schema(tmp_path):
    """A database created before the snapshot columns existed (Service v1's
    initial release) must gain them via ALTER TABLE, without losing existing
    rows or raising."""
    import sqlite3

    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE template_decisions (
            decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER NOT NULL UNIQUE,
            preview_id INTEGER NOT NULL,
            final_template_id TEXT NOT NULL,
            final_slots_json TEXT NOT NULL,
            final_rendered_text TEXT NOT NULL,
            generation_method TEXT NOT NULL,
            confirmed_by INTEGER NOT NULL,
            confirmed_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO template_decisions (message_id, preview_id, final_template_id, "
        "final_slots_json, final_rendered_text, generation_method, confirmed_by, confirmed_at) "
        "VALUES (1, 1, 'HEAVY_RAIN_CLEARED', '{}', '기존 문안', 'rule', 111, '2026-01-01T00:00:00+09:00')"
    )
    conn.commit()
    conn.close()

    migrated = Database(path)  # must not raise
    row = migrated._conn.execute("SELECT * FROM template_decisions WHERE message_id = 1").fetchone()
    assert row["final_rendered_text"] == "기존 문안"
    assert row["source_id_snapshot"] is None
    migrated.close()


def test_supersede_active_previews_only_affects_active_statuses_for_that_user(db, make_record):
    record = make_record(source_id="DS1")
    db.insert(record)
    active_id = db.insert_preview(
        message_id=record.internal_id,
        selected_template_id="A",
        selected_by=111,
        extraction_method="rule",
        extracted_slots_json="{}",
        rendered_text="x",
        missing_slots_json="[]",
        status="rule_preview",
        interaction_chat_id="100",
    )
    confirmed_id = db.insert_preview(
        message_id=record.internal_id,
        selected_template_id="B",
        selected_by=111,
        extraction_method="rule",
        extracted_slots_json="{}",
        rendered_text="y",
        missing_slots_json="[]",
        status="confirmed",
        interaction_chat_id="100",
    )
    other_user_active_id = db.insert_preview(
        message_id=record.internal_id,
        selected_template_id="C",
        selected_by=222,
        extraction_method="rule",
        extracted_slots_json="{}",
        rendered_text="z",
        missing_slots_json="[]",
        status="rule_preview",
        interaction_chat_id="200",
    )
    # Same user in a DIFFERENT chat must not be superseded either.
    same_user_other_chat_id = db.insert_preview(
        message_id=record.internal_id,
        selected_template_id="D",
        selected_by=111,
        extraction_method="rule",
        extracted_slots_json="{}",
        rendered_text="w",
        missing_slots_json="[]",
        status="rule_preview",
        interaction_chat_id="999",
    )

    db.supersede_active_previews(
        message_id=record.internal_id, selected_by=111, interaction_chat_id="100"
    )

    assert db.get_preview(active_id).status == "superseded"
    assert db.get_preview(confirmed_id).status == "confirmed"
    assert db.get_preview(other_user_active_id).status == "rule_preview"
    assert db.get_preview(same_user_other_chat_id).status == "rule_preview"


def test_confirmed_decision_survives_message_retention_cleanup(db, make_record):
    """A confirmed template_decisions row must outlive `messages` retention
    cleanup — message_id columns on template_* tables are deliberately not
    enforced foreign keys (see app/database.py module docstring)."""
    old_sent_at = datetime(2020, 1, 1, tzinfo=SEOUL_TZ)
    record = make_record(source_id="DS-OLD", sent_at=old_sent_at)
    db.insert(record)
    db.upsert_decision(
        message_id=record.internal_id,
        preview_id=1,
        final_template_id="HEAVY_RAIN_CLEARED",
        final_slots_json="{}",
        final_rendered_text="보관될 문안",
        generation_method="rule",
        confirmed_by=111,
        interaction_chat_id="100",
    )

    # Must not raise sqlite3.IntegrityError (FOREIGN KEY constraint failed).
    db.cleanup_execute(
        message_retention_days=1, run_history_retention_days=14, tombstone_retention_days=365
    )

    assert db._conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"] == 0
    row = db.get_decision_by_message_id(record.internal_id)
    assert row is not None
    assert row["final_rendered_text"] == "보관될 문안"
