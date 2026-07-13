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


def test_history_settings_defaults(tmp_path, monkeypatch):
    for key in (
        "HISTORY_DATABASE_PATH",
        "HISTORY_REQUEST_DELAY_SECONDS",
        "HISTORY_REQUEST_TIMEOUT_SECONDS",
        "HISTORY_MAX_RETRIES",
        "HISTORY_TARGET_COUNT",
    ):
        monkeypatch.delenv(key, raising=False)

    settings = load_settings(env_file=tmp_path / "missing.env")
    assert settings.history_database_path.name == "history_raw.db"
    assert settings.history_request_delay_seconds == 1.5
    assert settings.history_request_timeout_seconds == 20
    assert settings.history_max_retries == 3
    assert settings.history_target_count == 10000


def test_history_request_delay_out_of_range_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("HISTORY_REQUEST_DELAY_SECONDS", "0.1")
    with pytest.raises(ValueError):
        load_settings(env_file=tmp_path / "missing.env")

    monkeypatch.setenv("HISTORY_REQUEST_DELAY_SECONDS", "20")
    with pytest.raises(ValueError):
        load_settings(env_file=tmp_path / "missing.env")


def test_history_database_path_relative_resolved_under_project_root(tmp_path, monkeypatch):
    monkeypatch.setenv("HISTORY_DATABASE_PATH", "data/custom_history.db")
    settings = load_settings(env_file=tmp_path / "missing.env")
    assert settings.history_database_path.is_absolute()
    assert settings.history_database_path.name == "custom_history.db"
