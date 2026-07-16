from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.database import Database
from app.models import cross_source_equivalent_ids, strip_source_namespace

SEOUL_TZ = ZoneInfo("Asia/Seoul")


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "test.db")
    yield database
    database.close()


# -- app.models identity helpers ----------------------------------------------


def test_strip_source_namespace_mois():
    assert strip_source_namespace("MOIS:261088") == "261088"


def test_strip_source_namespace_safekorea():
    assert strip_source_namespace("SAFEKOREA:261088") == "261088"


def test_strip_source_namespace_legacy_id_unchanged():
    assert strip_source_namespace("DS1") == "DS1"


def test_cross_source_equivalent_ids_includes_both_namespaces():
    ids = cross_source_equivalent_ids("MOIS:261088")
    assert set(ids) == {"MOIS:261088", "SAFEKOREA:261088"}


def test_cross_source_equivalent_ids_from_safekorea_side():
    ids = cross_source_equivalent_ids("SAFEKOREA:261088")
    assert set(ids) == {"MOIS:261088", "SAFEKOREA:261088"}


def test_cross_source_equivalent_ids_legacy_id_still_matches_itself():
    """A pre-v0.5.0 non-namespaced id must still be found by its own value —
    this was a real bug: the first implementation dropped the original id
    when generating namespaced variants."""
    ids = cross_source_equivalent_ids("DS1")
    assert "DS1" in ids


# -- Database.is_known cross-source coverage (the runtime dedup path) --------


def test_mois_first_then_safekorea_is_recognized_as_known(db, make_record):
    mois_record = make_record(source_id="MOIS:261088", body="본문 A")
    db.insert(mois_record)

    safekorea_record = make_record(
        source_id="SAFEKOREA:261088", body="[노원구] 본문 A"
    )  # different body text (org-name prefix), same underlying message
    assert db.is_known(safekorea_record)


def test_safekorea_first_then_mois_is_recognized_as_known(db, make_record):
    safekorea_record = make_record(source_id="SAFEKOREA:261088", body="[노원구] 본문 A")
    db.insert(safekorea_record)

    mois_record = make_record(source_id="MOIS:261088", body="본문 A")
    assert db.is_known(mois_record)


def test_different_numeric_ids_are_not_confused(db, make_record):
    db.insert(make_record(source_id="MOIS:100", body="본문 A"))
    other = make_record(source_id="SAFEKOREA:200", body="본문 B")
    assert not db.is_known(other)


def test_same_text_different_sent_at_is_not_incorrectly_deduplicated(db, make_record):
    """Same body/region but a different source_id AND a different sent_at
    (so raw_hash also differs) must not collide."""
    db.insert(
        make_record(
            source_id="MOIS:1",
            body="동일 본문",
            sent_at=datetime(2026, 7, 14, 10, 0, tzinfo=SEOUL_TZ),
        )
    )
    later = make_record(
        source_id="MOIS:2",
        body="동일 본문",
        sent_at=datetime(2026, 7, 14, 11, 0, tzinfo=SEOUL_TZ),
    )
    assert not db.is_known(later)


def test_same_sent_at_different_body_is_not_incorrectly_deduplicated(db, make_record):
    same_time = datetime(2026, 7, 14, 10, 0, tzinfo=SEOUL_TZ)
    db.insert(make_record(source_id="MOIS:1", body="본문 A", sent_at=same_time))
    other = make_record(source_id="MOIS:2", body="완전히 다른 본문", sent_at=same_time)
    assert not db.is_known(other)


def test_api_recovery_after_fallback_does_not_resend(db, make_record):
    """Simulates: fallback delivers a message first (SAFEKOREA:x), then the
    API recovers and returns the same message (MOIS:x) on a later cycle —
    the second collection must be recognized as already known."""
    fallback_record = make_record(source_id="SAFEKOREA:500", body="[구청] 재난 안내")
    db.insert(fallback_record)

    recovered_api_record = make_record(source_id="MOIS:500", body="재난 안내")
    assert db.is_known(recovered_api_record)


def test_tombstoned_message_still_recognized_across_source(db, make_record):
    """A message deleted by retention cleanup is tombstoned by (source_id,
    raw_hash); a same-window arrival under the other namespace must still be
    caught so it is not resent after retention."""
    record = make_record(source_id="MOIS:700", body="본문")
    db.insert(record)
    # Simulate retention cleanup tombstoning it and removing the row.
    db._execute_write(
        "INSERT OR REPLACE INTO tombstones (source_id, raw_hash, expired_at) VALUES (?, ?, ?)",
        (record.source_id, record.raw_hash, datetime.now(tz=SEOUL_TZ).isoformat()),
    )
    db._execute_write("DELETE FROM messages WHERE source_id = ?", (record.source_id,))

    safekorea_equivalent = make_record(source_id="SAFEKOREA:700", body="[구청] 본문")
    assert db.is_known(safekorea_equivalent)


# -- find_equivalent_recent_message (cutover --inspect reporting path) -------


def test_find_equivalent_recent_message_matches_cross_namespace(db, make_record):
    now = datetime.now(tz=SEOUL_TZ)
    db.insert(make_record(source_id="MOIS:800", body="본문", sent_at=now))
    candidate = make_record(source_id="SAFEKOREA:800", body="[구청] 본문", sent_at=now)
    found = db.find_equivalent_recent_message(candidate, lookback_days=8)
    assert found is not None
    assert found.source_id == "MOIS:800"


def test_find_equivalent_recent_message_respects_lookback_window(db, make_record):
    old = datetime.now(tz=SEOUL_TZ) - timedelta(days=30)
    db.insert(make_record(source_id="MOIS:900", body="오래된 본문", sent_at=old))
    candidate = make_record(source_id="SAFEKOREA:900", body="[구청] 오래된 본문", sent_at=old)
    found = db.find_equivalent_recent_message(candidate, lookback_days=8)
    assert found is None


def test_find_equivalent_recent_message_returns_none_for_genuinely_new(db, make_record):
    candidate = make_record(source_id="MOIS:999", body="신규 본문")
    assert db.find_equivalent_recent_message(candidate, lookback_days=8) is None
