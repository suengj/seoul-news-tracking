from __future__ import annotations

import pytest

from app.config import ConfigError, load_settings

ALL_ENV_KEYS = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "TELEGRAM_ALLOWED_USER_IDS",
    "TELEGRAM_SEND_ENABLED",
    "DATABASE_PATH",
    "LOG_LEVEL",
    "POLL_INTERVAL_SECONDS",
    "STATUS_STALE_AFTER_MINUTES",
    "MESSAGE_RETENTION_DAYS",
    "RUN_HISTORY_RETENTION_DAYS",
    "STORE_SUCCESSFUL_NOOP_RUNS",
    "CLEANUP_INTERVAL_HOURS",
    "TOMBSTONE_RETENTION_DAYS",
    "LOCAL_SHUTDOWN_COMMAND_ENABLED",
)


def test_missing_env_vars_produce_safe_defaults(tmp_path, monkeypatch):
    for key in ALL_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    missing_env_file = tmp_path / "does-not-exist.env"
    settings = load_settings(env_file=missing_env_file)

    assert settings.telegram_bot_token == ""
    assert settings.telegram_chat_id == ""
    assert settings.telegram_allowed_user_ids == ()
    assert settings.telegram_send_enabled is False
    assert not settings.telegram_configured
    assert settings.log_level == "INFO"
    assert settings.poll_interval_seconds == 60
    assert settings.status_stale_after_minutes == 5
    assert settings.message_retention_days == 90
    assert settings.run_history_retention_days == 14
    assert settings.store_successful_noop_runs is False
    assert settings.cleanup_interval_hours == 24
    assert settings.tombstone_retention_days == 365
    assert settings.local_shutdown_command_enabled is False


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


@pytest.mark.parametrize(
    "env_key,valid_value,expected",
    [
        ("MESSAGE_RETENTION_DAYS", "30", 30),
        ("MESSAGE_RETENTION_DAYS", "90", 90),
        ("RUN_HISTORY_RETENTION_DAYS", "14", 14),
        ("CLEANUP_INTERVAL_HOURS", "24", 24),
        ("POLL_INTERVAL_SECONDS", "120", 120),
        ("STATUS_STALE_AFTER_MINUTES", "10", 10),
    ],
)
def test_retention_and_interval_settings_parsed(
    tmp_path, monkeypatch, env_key, valid_value, expected
):
    monkeypatch.setenv(env_key, valid_value)
    settings = load_settings(env_file=tmp_path / "missing.env")
    attr = env_key.lower()
    assert getattr(settings, attr) == expected


@pytest.mark.parametrize(
    "env_key,invalid_value",
    [
        ("MESSAGE_RETENTION_DAYS", "0"),
        ("MESSAGE_RETENTION_DAYS", "3651"),
        ("MESSAGE_RETENTION_DAYS", "not-a-number"),
        ("RUN_HISTORY_RETENTION_DAYS", "0"),
        ("RUN_HISTORY_RETENTION_DAYS", "366"),
        ("CLEANUP_INTERVAL_HOURS", "0"),
        ("CLEANUP_INTERVAL_HOURS", "169"),
        ("POLL_INTERVAL_SECONDS", "0"),
        ("POLL_INTERVAL_SECONDS", "4"),
        ("STATUS_STALE_AFTER_MINUTES", "0"),
    ],
)
def test_retention_and_interval_settings_reject_out_of_range(
    tmp_path, monkeypatch, env_key, invalid_value
):
    monkeypatch.setenv(env_key, invalid_value)
    with pytest.raises(ConfigError):
        load_settings(env_file=tmp_path / "missing.env")


def test_store_successful_noop_runs_and_local_shutdown_bool_parsing(tmp_path, monkeypatch):
    monkeypatch.setenv("STORE_SUCCESSFUL_NOOP_RUNS", "true")
    monkeypatch.setenv("LOCAL_SHUTDOWN_COMMAND_ENABLED", "true")
    settings = load_settings(env_file=tmp_path / "missing.env")
    assert settings.store_successful_noop_runs is True
    assert settings.local_shutdown_command_enabled is True
