from __future__ import annotations

from app.history_models import HistoricalRawRecord, compute_history_raw_hash


def _make(**overrides):
    defaults = dict(
        source="test-source",
        source_id="123",
        sent_at_raw="2026/07/13 10:00:00",
        sent_at=None,
        sender_raw="관리자",
        region_raw="테스트구",
        title_raw="title",
        body_raw="body text",
        list_url="https://example.test/list",
        detail_url="https://example.test/detail?sn=123",
        raw_payload={},
        source_page=1,
        source_position=1,
    )
    defaults.update(overrides)
    return HistoricalRawRecord(**defaults)


def test_raw_hash_uses_source_id_when_available():
    record_a = _make(source_id="123", body_raw="body one")
    record_b = _make(source_id="123", body_raw="a completely different body")
    assert record_a.raw_hash == record_b.raw_hash  # hash is source_id-keyed, not content-keyed


def test_raw_hash_differs_across_source_ids():
    record_a = _make(source_id="123")
    record_b = _make(source_id="456")
    assert record_a.raw_hash != record_b.raw_hash


def test_raw_hash_falls_back_to_content_without_source_id():
    hash_a = compute_history_raw_hash(
        source_id=None, sender_raw="s", sent_at_raw="t", region_raw="r", body_raw="body"
    )
    hash_b = compute_history_raw_hash(
        source_id=None, sender_raw="s", sent_at_raw="t", region_raw="r", body_raw="different body"
    )
    assert hash_a != hash_b


def test_raw_hash_fallback_matches_for_identical_content():
    hash_a = compute_history_raw_hash(
        source_id=None, sender_raw="s", sent_at_raw="t", region_raw="r", body_raw="body"
    )
    hash_b = compute_history_raw_hash(
        source_id=None, sender_raw="s", sent_at_raw="t", region_raw="r", body_raw="body"
    )
    assert hash_a == hash_b
