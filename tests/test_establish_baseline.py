from __future__ import annotations

import pytest

from app.collector import METHOD_MOIS_API, CollectionResult, CollectorError
from app.commands import establish_baseline
from app.database import Database


@pytest.fixture
def env_setup(tmp_path, monkeypatch):
    db_path = tmp_path / "establish_baseline_test.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "fake-chat")
    monkeypatch.setenv("TELEGRAM_SEND_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "")
    monkeypatch.setenv("LOG_LEVEL", "ERROR")
    return db_path


def _mock_collect(monkeypatch, records):
    def fake_fetch(settings, **kwargs):
        return CollectionResult(
            records=records,
            method=METHOD_MOIS_API,
            fetched_count=len(records),
            full_text_confirmed=True,
        )

    monkeypatch.setattr(establish_baseline, "fetch_records", fake_fetch)


def test_baseline_marks_source_bootstrap_completed(env_setup, monkeypatch, make_record):
    """A fresh database's baseline run is itself a safe v0.5.0 source
    cutover — without marking source_bootstrap_completed here, poll_once's
    cutover gate would keep demanding an explicit `source_cutover_mois
    --bootstrap` run even though this command already did the equivalent
    work (insert-only, nothing sent)."""
    _mock_collect(monkeypatch, [make_record(source_id="MOIS:1"), make_record(source_id="MOIS:2")])

    rc = establish_baseline.main()
    assert rc == 0

    db = Database(env_setup)
    assert not db.is_empty
    assert db.is_source_bootstrap_completed() is True
    db.close()


def test_baseline_is_idempotent_on_non_empty_db(env_setup, monkeypatch, make_record):
    _mock_collect(monkeypatch, [make_record(source_id="MOIS:1")])
    assert establish_baseline.main() == 0

    _mock_collect(monkeypatch, [make_record(source_id="MOIS:2")])
    rc = establish_baseline.main()
    assert rc == 0

    db = Database(env_setup)
    assert db.known_source_ids() == {"MOIS:1"}  # second run made no changes
    db.close()


def test_baseline_failure_leaves_bootstrap_incomplete(env_setup, monkeypatch):
    def failing_fetch(settings, **kwargs):
        raise CollectorError("both sources failed")

    monkeypatch.setattr(establish_baseline, "fetch_records", failing_fetch)

    rc = establish_baseline.main()
    assert rc == 1

    db = Database(env_setup)
    assert db.is_source_bootstrap_completed() is False
    db.close()
