"""Local administrative shared-poller control command (v0.4.1).

`app/commands/poller_control.py` is the only explicit control over the shared
collector's `system_state.polling_enabled` flag. These tests drive its command
functions directly against a temporary SQLite database — no Telegram, OpenAI,
or SafeCity network access, and no Telegram configuration is required.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.commands import poller_control
from app.database import SEOUL_TZ, Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "pc.db")
    yield database
    database.close()


def _run_history_rows(db: Database):
    return db._conn.execute(
        "SELECT run_type, detail FROM run_history ORDER BY run_id ASC"
    ).fetchall()


# --- status ------------------------------------------------------------------


def test_status_when_enabled_and_fresh_is_healthy(db, capsys):
    now = datetime(2026, 7, 15, 12, 0, 0, tzinfo=SEOUL_TZ)
    db.record_poll_success(now=now - timedelta(minutes=1))
    rc = poller_control.cmd_status(db, stale_after_minutes=15, now=now)
    out = capsys.readouterr().out
    assert rc == 0
    assert "polling_enabled        : True" in out
    assert "health                 : healthy" in out


def test_status_when_enabled_but_stale(db, capsys):
    now = datetime(2026, 7, 15, 12, 0, 0, tzinfo=SEOUL_TZ)
    db.record_poll_success(now=now - timedelta(minutes=60))
    poller_control.cmd_status(db, stale_after_minutes=15, now=now)
    out = capsys.readouterr().out
    assert "polling_enabled        : True" in out
    assert "health                 : stale" in out


def test_status_when_disabled(db, capsys):
    db.pause_polling(actor_user_id=0)
    poller_control.cmd_status(db, stale_after_minutes=15)
    out = capsys.readouterr().out
    assert "polling_enabled        : False" in out
    assert "health                 : disabled" in out


def test_status_never_leaks_actor_identifier(db, capsys):
    # A real operator id paused it; status must not print that id.
    db.pause_polling(actor_user_id=987654321)
    poller_control.cmd_status(db, stale_after_minutes=15)
    out = capsys.readouterr().out
    assert "987654321" not in out
    assert "paused_by" not in out
    assert "resumed_by" not in out


def test_status_is_read_only(db, capsys):
    before = db.get_system_state()
    poller_control.cmd_status(db, stale_after_minutes=15)
    after = db.get_system_state()
    assert before == after
    # No run_history control event is written by a status read.
    assert _run_history_rows(db) == []


# --- resume ------------------------------------------------------------------


def test_resume_changes_disabled_to_enabled(db, capsys):
    db.pause_polling(actor_user_id=0)
    assert db.is_polling_enabled() is False
    rc = poller_control.cmd_resume(db)
    out = capsys.readouterr().out
    assert rc == 0
    assert db.is_polling_enabled() is True
    assert "RESUMED" in out


def test_resume_is_idempotent(db, capsys):
    assert db.is_polling_enabled() is True  # fresh DB defaults to enabled
    poller_control.cmd_resume(db)
    out = capsys.readouterr().out
    assert db.is_polling_enabled() is True
    assert "already enabled" in out


# --- pause -------------------------------------------------------------------


def test_pause_changes_enabled_to_disabled(db, capsys):
    assert db.is_polling_enabled() is True
    rc = poller_control.cmd_pause(db)
    out = capsys.readouterr().out
    assert rc == 0
    assert db.is_polling_enabled() is False
    assert "PAUSED" in out
    # Must clearly mark itself as a local admin command, not a personal mute.
    assert "not a personal Telegram mute" in out


def test_pause_is_idempotent(db, capsys):
    db.pause_polling(actor_user_id=0)
    poller_control.cmd_pause(db)
    out = capsys.readouterr().out
    assert db.is_polling_enabled() is False
    assert "already paused" in out


# --- audit / attribution -----------------------------------------------------


def test_admin_action_is_recorded_with_actor_zero(db, capsys):
    poller_control.cmd_pause(db)
    poller_control.cmd_resume(db)
    rows = _run_history_rows(db)
    run_types = [r["run_type"] for r in rows]
    assert "pause" in run_types and "resume" in run_types
    # Non-user administrative actor marker (0), never a real Telegram user id.
    for r in rows:
        assert "actor_user_id=0" in r["detail"]


# --- main() wiring: no Telegram config / no network -------------------------


def test_main_requires_no_telegram_config(tmp_path, monkeypatch, capsys):
    # The autouse conftest fixture already points PROJECT_ROOT/.env at an empty
    # tmp dir, so load_settings() sees NO Telegram token/chat/allowed users.
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "main.db"))
    for key in (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "TELEGRAM_ALLOWED_USER_IDS",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)

    assert poller_control.main(["status"]) == 0
    assert poller_control.main(["pause"]) == 0
    assert poller_control.main(["resume"]) == 0
    out = capsys.readouterr().out
    assert "shared SafeCity poller state" in out


def test_main_status_pause_resume_roundtrip_via_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "cli.db"))
    poller_control.main(["pause"])
    poller_control.main(["status"])
    mid_out = capsys.readouterr().out
    assert "polling_enabled        : False" in mid_out
    poller_control.main(["resume"])
    poller_control.main(["status"])
    end_out = capsys.readouterr().out
    assert "polling_enabled        : True" in end_out
