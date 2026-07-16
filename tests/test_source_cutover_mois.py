from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.commands import source_cutover_mois
from app.database import Database

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load_json(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("app.mois_api.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("app.safekorea_fallback.time.sleep", lambda _seconds: None)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "test.db")
    yield database
    database.close()


@pytest.fixture
def settings(make_settings, tmp_path):
    return make_settings(
        database_path=tmp_path / "test.db",
        safetydata_service_key="TEST_KEY",
        safekorea_fallback_enabled=True,
    )


def _mock_collect(monkeypatch, records):
    """Patch collector.fetch_records used inside source_cutover_mois so
    bootstrap/inspect never make real network calls."""
    from app.collector import METHOD_MOIS_API, CollectionResult

    def fake_fetch(settings, **kwargs):
        return CollectionResult(
            records=records,
            method=METHOD_MOIS_API,
            fetched_count=len(records),
            full_text_confirmed=True,
        )

    monkeypatch.setattr(source_cutover_mois, "fetch_records", fake_fetch)


def test_bootstrap_on_fresh_empty_db_marks_completed_without_inserting(
    db, settings, monkeypatch, make_record
):
    db.pause_polling(actor_user_id=1)  # bootstrap requires the shared collector paused first
    _mock_collect(monkeypatch, [make_record(source_id="MOIS:1")])
    rc = source_cutover_mois.run_bootstrap(settings)
    assert rc == 0

    db2 = Database(settings.database_path)
    assert db2.is_empty  # nothing inserted for a genuinely empty DB
    assert db2.is_source_bootstrap_completed() is True
    db2.close()


def test_bootstrap_refuses_while_shared_collector_enabled(db, settings, monkeypatch, make_record):
    db.insert(make_record(source_id="LEGACY1", body="legacy 본문"))  # pre-existing (non-empty) DB
    db.close()
    _mock_collect(monkeypatch, [make_record(source_id="MOIS:1", body="new 본문")])

    rc = source_cutover_mois.run_bootstrap(settings)
    assert rc == 1  # polling_enabled defaults to True

    db2 = Database(settings.database_path)
    assert db2.is_source_bootstrap_completed() is False
    db2.close()


def test_bootstrap_registers_baseline_without_sending(db, settings, monkeypatch, make_record):
    db.insert(make_record(source_id="LEGACY1", body="legacy 본문"))
    db.pause_polling(actor_user_id=1)
    db.close()

    new_records = [
        make_record(source_id="MOIS:100", body="본문 100"),
        make_record(source_id="MOIS:101", body="본문 101"),
    ]
    _mock_collect(monkeypatch, new_records)

    rc = source_cutover_mois.run_bootstrap(settings)
    assert rc == 0

    db2 = Database(settings.database_path)
    assert db2.is_source_bootstrap_completed() is True
    assert db2.known_source_ids() == {"LEGACY1", "MOIS:100", "MOIS:101"}
    # No deliveries or suggestions created by bootstrap.
    row = db2._conn.execute("SELECT COUNT(*) c FROM telegram_deliveries").fetchone()
    assert row["c"] == 0
    row = db2._conn.execute("SELECT COUNT(*) c FROM template_suggestions").fetchone()
    assert row["c"] == 0
    # Registered as baseline.
    for source_id in ("MOIS:100", "MOIS:101"):
        cur = db2._conn.execute(
            "SELECT is_baseline FROM messages WHERE source_id = ?", (source_id,)
        )
        assert cur.fetchone()["is_baseline"] == 1
    db2.close()


def test_bootstrap_is_idempotent(db, settings, monkeypatch, make_record):
    db.insert(make_record(source_id="LEGACY1", body="legacy 본문"))
    db.pause_polling(actor_user_id=1)
    db.close()

    new_records = [make_record(source_id="MOIS:100", body="본문 100")]
    _mock_collect(monkeypatch, new_records)

    rc1 = source_cutover_mois.run_bootstrap(settings)
    assert rc1 == 0
    db_after_first = Database(settings.database_path)
    count_after_first = len(db_after_first.known_source_ids())
    db_after_first.close()

    rc2 = source_cutover_mois.run_bootstrap(settings)
    assert rc2 == 0
    db_after_second = Database(settings.database_path)
    count_after_second = len(db_after_second.known_source_ids())
    db_after_second.close()

    assert count_after_first == count_after_second == 2  # LEGACY1 + MOIS:100, no duplicate


def test_inspect_is_read_only(db, settings, monkeypatch, make_record):
    db.insert(make_record(source_id="LEGACY1"))
    db.close()
    _mock_collect(monkeypatch, [make_record(source_id="MOIS:100")])

    rc = source_cutover_mois.run_inspect(settings)
    assert rc == 0

    db2 = Database(settings.database_path)
    assert db2.known_source_ids() == {"LEGACY1"}  # unchanged — inspect never inserts
    assert db2.is_source_bootstrap_completed() is False
    db2.close()


def test_inspect_reports_collector_error_without_crashing(db, settings, monkeypatch):
    from app.collector import CollectorError

    def failing_fetch(settings, **kwargs):
        raise CollectorError("both sources failed")

    monkeypatch.setattr(source_cutover_mois, "fetch_records", failing_fetch)
    rc = source_cutover_mois.run_inspect(settings)
    assert rc == 1


def test_missing_key_stops_before_any_inspection(capsys):
    # The autouse `_never_load_the_real_dotenv` fixture redirects
    # config.PROJECT_ROOT to an empty tmp dir, so load_settings() here sees
    # no SAFETYDATA_SERVICE_KEY at all — main() must refuse before touching
    # the network or the database.
    rc = source_cutover_mois.main(["--inspect"])
    assert rc == 1
    captured = capsys.readouterr()
    assert "SAFETYDATA_SERVICE_KEY is missing" in captured.err
