from __future__ import annotations

import pytest

from app.config import ConfigError, load_settings


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


# --- Service v1: polling / retention / AI settings ---------------------------


def test_service_v1_defaults(tmp_path, monkeypatch):
    for key in (
        "POLL_INTERVAL_SECONDS",
        "STATUS_STALE_AFTER_MINUTES",
        "MESSAGE_RETENTION_DAYS",
        "RUN_HISTORY_RETENTION_DAYS",
        "CLEANUP_INTERVAL_HOURS",
        "TOMBSTONE_RETENTION_DAYS",
        "AI_ENABLED",
        "OPENAI_MODEL",
        "OPENAI_TIMEOUT_SECONDS",
        "OPENAI_MAX_RETRIES",
    ):
        monkeypatch.delenv(key, raising=False)

    settings = load_settings(env_file=tmp_path / "missing.env")
    assert settings.poll_interval_seconds == 300
    assert settings.status_stale_after_minutes == 15
    assert settings.message_retention_days == 90
    assert settings.run_history_retention_days == 14
    assert settings.cleanup_interval_hours == 24
    assert settings.tombstone_retention_days == 365
    assert settings.ai_enabled is False
    assert settings.openai_api_key == ""
    assert settings.openai_model == "gpt-5-mini"
    assert settings.openai_timeout_seconds == 30
    assert settings.openai_max_retries == 2
    assert settings.ai_configured is False


def test_runtime_identity_is_loaded_from_explicit_environment_values(tmp_path, monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_LABEL", "linux-production")
    monkeypatch.setenv("RUNTIME_MODE", "systemd")

    settings = load_settings(env_file=tmp_path / "missing.env")

    assert settings.deployment_label == "linux-production"
    assert settings.runtime_mode == "systemd"


def test_cutover_fence_requires_explicit_shared_path_and_host_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("CUTOVER_FENCE_PATH", "shared/cutover-fence")
    monkeypatch.setenv("CUTOVER_HOST_ID", "linux")

    settings = load_settings(env_file=tmp_path / "missing.env")

    assert settings.cutover_fence_path is not None
    assert settings.cutover_fence_path.is_absolute()
    assert settings.cutover_fence_path.name == "cutover-fence"
    assert settings.cutover_host_id == "linux"


def test_poll_interval_out_of_range_raises_config_error(tmp_path, monkeypatch):
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "1")
    with pytest.raises(ConfigError):
        load_settings(env_file=tmp_path / "missing.env")


def test_status_stale_after_minutes_out_of_range_raises_config_error(tmp_path, monkeypatch):
    monkeypatch.setenv("STATUS_STALE_AFTER_MINUTES", "0")
    with pytest.raises(ConfigError):
        load_settings(env_file=tmp_path / "missing.env")


def test_ai_enabled_requires_key_to_be_considered_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_ENABLED", "true")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    settings = load_settings(env_file=tmp_path / "missing.env")
    assert settings.ai_enabled is True
    assert settings.ai_configured is False  # no key supplied yet

    monkeypatch.setenv("OPENAI_API_KEY", "sk-real")
    settings = load_settings(env_file=tmp_path / "missing.env")
    assert settings.ai_configured is True
