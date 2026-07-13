from __future__ import annotations

from pathlib import Path

import pytest

from app.commands import backfill_history
from app.history_backfill import BackfillProgress, HistoryBackfillError
from app.history_database import open_history_database


@pytest.fixture
def history_db_env(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "history.db"
    monkeypatch.setenv("HISTORY_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "")
    return db_path


def test_status_with_no_runs_reports_zero(history_db_env, capsys):
    exit_code = backfill_history.main(["--status"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "no backfill run has been started yet" in out


def test_status_reports_existing_run(history_db_env, capsys):
    with open_history_database(history_db_env) as db:
        db.start_run(target_count=10000)

    exit_code = backfill_history.main(["--status"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "status           : running" in out
    assert "target_count     : 10,000" in out


def test_main_uses_cli_overrides_over_settings(history_db_env, monkeypatch):
    captured = {}

    def fake_run_backfill(db, **kwargs):
        captured.update(kwargs)
        return BackfillProgress(
            run_id=1, current_page=1, pages_processed=0, fetched_count=0,
            inserted_count=0, duplicate_count=0, malformed_count=0, error_count=0,
            retry_count=0, target_count=kwargs["target_count"], unique_count=0,
            status="completed",
        )

    monkeypatch.setattr(backfill_history, "run_backfill", fake_run_backfill)
    exit_code = backfill_history.main(["--target-count", "42", "--delay-seconds", "2.0"])
    assert exit_code == 0
    assert captured["target_count"] == 42
    assert captured["delay_seconds"] == 2.0
    assert captured["resume"] is False


def test_main_propagates_resume_error_as_exit_code(history_db_env, monkeypatch):
    def fake_run_backfill(db, **kwargs):
        raise HistoryBackfillError("no active run")

    monkeypatch.setattr(backfill_history, "run_backfill", fake_run_backfill)
    exit_code = backfill_history.main(["--resume"])
    assert exit_code == 1


def test_main_returns_nonzero_when_run_does_not_complete(history_db_env, monkeypatch):
    def fake_run_backfill(db, **kwargs):
        return BackfillProgress(
            run_id=1, current_page=3, pages_processed=3, fetched_count=30,
            inserted_count=10, duplicate_count=0, malformed_count=0, error_count=1,
            retry_count=0, target_count=100, unique_count=10, status="failed",
        )

    monkeypatch.setattr(backfill_history, "run_backfill", fake_run_backfill)
    exit_code = backfill_history.main([])
    assert exit_code == 1
