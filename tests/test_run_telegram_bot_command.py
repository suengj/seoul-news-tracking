from __future__ import annotations

import pytest

from app.commands import run_telegram_bot
from app.process_lock import SingleInstanceLock


@pytest.fixture
def env_setup(tmp_path, monkeypatch):
    db_path = tmp_path / "bot_cmd_test.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "fake-chat")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "111")
    monkeypatch.setenv("LOG_LEVEL", "ERROR")
    return db_path


def test_refuses_to_start_without_telegram_config(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "x.db"))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    rc = run_telegram_bot.main()
    assert rc == 1


def test_refuses_to_start_second_instance(env_setup, monkeypatch):
    lock_path = env_setup.parent / "run_telegram_bot.lock"
    held = SingleInstanceLock(lock_path)
    held.acquire()
    try:
        rc = run_telegram_bot.main()
        assert rc == 1
    finally:
        held.release()
