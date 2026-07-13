from __future__ import annotations

import pytest

from app.config import load_settings


def test_missing_env_vars_produce_safe_defaults(tmp_path, monkeypatch):
    for key in (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "TELEGRAM_ALLOWED_USER_IDS",
        "TELEGRAM_SEND_ENABLED",
        "DATABASE_PATH",
        "LOG_LEVEL",
    ):
        monkeypatch.delenv(key, raising=False)

    missing_env_file = tmp_path / "does-not-exist.env"
    settings = load_settings(env_file=missing_env_file)

    assert settings.telegram_bot_token == ""
    assert settings.telegram_chat_id == ""
    assert settings.telegram_allowed_user_ids == ()
    assert settings.telegram_send_enabled is False
    assert not settings.telegram_configured
    assert settings.log_level == "INFO"


def test_telegram_allowed_user_ids_parsed_as_ints(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "111, 222,333")
    settings = load_settings(env_file=tmp_path / "missing.env")
    assert settings.telegram_allowed_user_ids == (111, 222, 333)


def test_telegram_allowed_user_ids_invalid_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "not-an-id")
    with pytest.raises(ValueError):
        load_settings(env_file=tmp_path / "missing.env")


def test_send_enabled_bool_parsing(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_SEND_ENABLED", "true")
    settings = load_settings(env_file=tmp_path / "missing.env")
    assert settings.telegram_send_enabled is True

    monkeypatch.setenv("TELEGRAM_SEND_ENABLED", "false")
    settings = load_settings(env_file=tmp_path / "missing.env")
    assert settings.telegram_send_enabled is False


def test_telegram_configured_requires_both_token_and_chat_id(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "abc")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    settings = load_settings(env_file=tmp_path / "missing.env")
    assert not settings.telegram_configured
