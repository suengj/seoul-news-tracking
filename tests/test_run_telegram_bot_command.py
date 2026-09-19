from __future__ import annotations

import pytest

from app.commands import run_telegram_bot
from app.cutover_fence import FENCE_REFUSAL_EXIT_CODE, CutoverFenceStore
from app.process_lock import SingleInstanceLock


@pytest.fixture
def env_setup(tmp_path, monkeypatch):
    db_path = tmp_path / "bot_cmd_test.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "fake-chat")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "111")
    monkeypatch.setenv("LOG_LEVEL", "ERROR")
    monkeypatch.setenv("CUTOVER_FENCE_PATH", str(tmp_path / "fence"))
    monkeypatch.setenv("CUTOVER_HOST_ID", "linux")
    return db_path


def _grant_linux_authority(fence_path):
    mac = CutoverFenceStore(fence_path, token="fake-token", host_id="mac")
    request_id = mac.request_cutover(
        outgoing_host="mac", incoming_host="linux", request_id="command-test-cutover"
    )
    mac.confirm_off(request_id=request_id, confirmed_local_off=True)


def test_refuses_to_start_without_telegram_config(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "x.db"))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    rc = run_telegram_bot.main()
    assert rc == 1


def test_refuses_to_start_second_instance(env_setup, monkeypatch):
    _grant_linux_authority(env_setup.parent / "fence")
    lock_path = env_setup.parent / "run_telegram_bot.lock"
    held = SingleInstanceLock(lock_path)
    held.acquire()
    try:
        rc = run_telegram_bot.main()
        assert rc == 1
    finally:
        held.release()


def test_missing_fence_refuses_before_bot_or_telegram_network(env_setup, monkeypatch, capsys):
    calls = []

    class ShouldNotConstruct:
        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("TelegramBotRunner must not be constructed")

    monkeypatch.setattr(run_telegram_bot, "TelegramBotRunner", ShouldNotConstruct)
    rc = run_telegram_bot.main()

    assert rc == FENCE_REFUSAL_EXIT_CODE
    assert calls == []
    assert "FENCE" in capsys.readouterr().err


def test_unreadable_authority_lock_returns_terminal_fence_code(env_setup, capsys):
    fence_path = env_setup.parent / "fence"
    fence_path.mkdir(parents=True)
    (fence_path / "authority.lock").mkdir()

    rc = run_telegram_bot.main()

    assert rc == FENCE_REFUSAL_EXIT_CODE
    assert "FENCE_LOCK_UNAVAILABLE" in capsys.readouterr().err


def test_fence_identity_lock_blocks_different_database_path(tmp_path, monkeypatch, capsys):
    fence_path = tmp_path / "fence"
    first_db = tmp_path / "state-a" / "bot.db"
    second_db = tmp_path / "state-b" / "bot.db"
    monkeypatch.setenv("DATABASE_PATH", str(second_db))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "fake-chat")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "111")
    monkeypatch.setenv("LOG_LEVEL", "ERROR")
    monkeypatch.setenv("CUTOVER_FENCE_PATH", str(fence_path))
    monkeypatch.setenv("CUTOVER_HOST_ID", "linux")
    _grant_linux_authority(fence_path)

    held = SingleInstanceLock(
        CutoverFenceStore(fence_path, token="fake-token", host_id="linux").consumer_lock_path
    )
    held.acquire()
    try:
        class ShouldNotConstruct:
            def __init__(self, *args, **kwargs):
                raise AssertionError("the second identity cannot reach the bot")

        monkeypatch.setattr(run_telegram_bot, "TelegramBotRunner", ShouldNotConstruct)
        assert first_db.parent != second_db.parent
        assert run_telegram_bot.main() == 1
        assert "already be running" in capsys.readouterr().err
    finally:
        held.release()


def test_valid_fence_is_claimed_before_runner_loop(env_setup, monkeypatch):
    fence_path = env_setup.parent / "fence"
    _grant_linux_authority(fence_path)
    calls = []

    class FakeBot:
        def __init__(self, settings, db):
            calls.append("constructed")

        def run_forever(self):
            calls.append("ran")

        def close(self):
            calls.append("closed")

        def stop(self):
            pass

    monkeypatch.setattr(run_telegram_bot, "TelegramBotRunner", FakeBot)
    rc = run_telegram_bot.main()

    assert rc == 0
    assert calls == ["constructed", "ran", "closed"]
    authority = (fence_path / "authority.json").read_text(encoding="utf-8")
    assert '"phase": "ACTIVE"' in authority
    assert list((fence_path / "consumers").glob("linux-*.lock"))
