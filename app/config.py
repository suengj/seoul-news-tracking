"""Environment-driven configuration for Seoul News Tracking (Service v1).

All values come from environment variables (optionally loaded from a local
`.env` via python-dotenv). Nothing here is hardcoded per-deployment secrets.
Every value is validated at load time with a clear error message — invalid
config must fail fast at startup, not surface as a confusing runtime error
later.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

SOURCE_PAGE_URL = "https://safecity.seoul.go.kr/news/dist/dust/newsDistDustList.page"
SOURCE_API_URL = "https://safecity.seoul.go.kr/disstr/selectDisstrSms.do"

HISTORY_LIST_URL = "https://www.safetydata.go.kr/disaster-data/disasterNotification"
HISTORY_DETAIL_URL = "https://www.safetydata.go.kr/disaster-data/disasterNotificationDetail"

HISTORY_REQUEST_DELAY_MIN = 0.5
HISTORY_REQUEST_DELAY_MAX = 10.0

TEMPLATES_PATH = PROJECT_ROOT / "config" / "message_templates.yaml"
ENTITY_DICTIONARY_PATH = PROJECT_ROOT / "config" / "entity_dictionary.yaml"

# Documented accepted ranges (see docs/database_retention.md and
# docs/local_runtime.md for rationale).
POLL_INTERVAL_SECONDS_RANGE = (5, 86400)
STATUS_STALE_AFTER_MINUTES_RANGE = (1, 1440)
MESSAGE_RETENTION_DAYS_RANGE = (1, 3650)
RUN_HISTORY_RETENTION_DAYS_RANGE = (1, 365)
CLEANUP_INTERVAL_HOURS_RANGE = (1, 168)
TOMBSTONE_RETENTION_DAYS_RANGE = (1, 3650)


class ConfigError(ValueError):
    """Raised for any invalid/out-of-range environment value at startup."""


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_user_ids(value: str) -> tuple[int, ...]:
    ids: list[int] = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        ids.append(int(part))
    return tuple(ids)


def _parse_int_in_range(name: str, raw: str, bounds: tuple[int, int]) -> int:
    low, high = bounds
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if not (low <= value <= high):
        raise ConfigError(f"{name} must be between {low} and {high}, got {value}")
    return value


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_chat_id: str
    telegram_allowed_user_ids: tuple[int, ...]
    telegram_send_enabled: bool
    database_path: Path
    log_level: str

    poll_interval_seconds: int
    status_stale_after_minutes: int

    message_retention_days: int
    run_history_retention_days: int
    store_successful_noop_runs: bool
    cleanup_interval_hours: int
    tombstone_retention_days: int

    local_shutdown_command_enabled: bool

    history_database_path: Path = Path("data/history_raw.db")
    history_request_delay_seconds: float = 1.5
    history_request_timeout_seconds: float = 20.0
    history_max_retries: int = 3
    history_target_count: int = 10000

    template_recommend_threshold: float = 0.85

    ai_enabled: bool = False
    openai_api_key: str = ""
    openai_model: str = "gpt-5-mini"
    openai_timeout_seconds: float = 30.0
    openai_max_retries: int = 2

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def ai_configured(self) -> bool:
        return self.ai_enabled and bool(self.openai_api_key)


def load_settings(env_file: Path | None = None) -> Settings:
    """Load settings from the environment, optionally loading a `.env` file first.

    `env_file` defaults to `<project_root>/.env` and is loaded if present.
    Missing `.env` is not an error — real deployments may set env vars directly.

    Raises ConfigError with a clear message for any invalid value.
    """
    dotenv_path = env_file if env_file is not None else PROJECT_ROOT / ".env"
    if dotenv_path.exists():
        load_dotenv(dotenv_path, override=False)

    database_path_raw = os.environ.get("DATABASE_PATH", "data/seoul_news.db")
    database_path = Path(database_path_raw)
    if not database_path.is_absolute():
        database_path = PROJECT_ROOT / database_path

    history_database_path_raw = os.environ.get("HISTORY_DATABASE_PATH", "data/history_raw.db")
    history_database_path = Path(history_database_path_raw)
    if not history_database_path.is_absolute():
        history_database_path = PROJECT_ROOT / history_database_path

    try:
        allowed_ids = _parse_user_ids(os.environ.get("TELEGRAM_ALLOWED_USER_IDS", ""))
    except ValueError as exc:
        raise ConfigError(
            "TELEGRAM_ALLOWED_USER_IDS must be a comma-separated list of integers"
        ) from exc

    history_request_delay_seconds = float(
        os.environ.get("HISTORY_REQUEST_DELAY_SECONDS", "1.5")
    )
    if not (HISTORY_REQUEST_DELAY_MIN <= history_request_delay_seconds <= HISTORY_REQUEST_DELAY_MAX):
        raise ConfigError(
            "HISTORY_REQUEST_DELAY_SECONDS must be between "
            f"{HISTORY_REQUEST_DELAY_MIN} and {HISTORY_REQUEST_DELAY_MAX}"
        )

    return Settings(
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
        telegram_allowed_user_ids=allowed_ids,
        telegram_send_enabled=_parse_bool(os.environ.get("TELEGRAM_SEND_ENABLED", "false")),
        database_path=database_path,
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        poll_interval_seconds=_parse_int_in_range(
            "POLL_INTERVAL_SECONDS",
            os.environ.get("POLL_INTERVAL_SECONDS", "300"),
            POLL_INTERVAL_SECONDS_RANGE,
        ),
        status_stale_after_minutes=_parse_int_in_range(
            "STATUS_STALE_AFTER_MINUTES",
            os.environ.get("STATUS_STALE_AFTER_MINUTES", "15"),
            STATUS_STALE_AFTER_MINUTES_RANGE,
        ),
        message_retention_days=_parse_int_in_range(
            "MESSAGE_RETENTION_DAYS",
            os.environ.get("MESSAGE_RETENTION_DAYS", "90"),
            MESSAGE_RETENTION_DAYS_RANGE,
        ),
        run_history_retention_days=_parse_int_in_range(
            "RUN_HISTORY_RETENTION_DAYS",
            os.environ.get("RUN_HISTORY_RETENTION_DAYS", "14"),
            RUN_HISTORY_RETENTION_DAYS_RANGE,
        ),
        store_successful_noop_runs=_parse_bool(
            os.environ.get("STORE_SUCCESSFUL_NOOP_RUNS", "false")
        ),
        cleanup_interval_hours=_parse_int_in_range(
            "CLEANUP_INTERVAL_HOURS",
            os.environ.get("CLEANUP_INTERVAL_HOURS", "24"),
            CLEANUP_INTERVAL_HOURS_RANGE,
        ),
        tombstone_retention_days=_parse_int_in_range(
            "TOMBSTONE_RETENTION_DAYS",
            os.environ.get("TOMBSTONE_RETENTION_DAYS", "365"),
            TOMBSTONE_RETENTION_DAYS_RANGE,
        ),
        local_shutdown_command_enabled=_parse_bool(
            os.environ.get("LOCAL_SHUTDOWN_COMMAND_ENABLED", "false")
        ),
        history_database_path=history_database_path,
        history_request_delay_seconds=history_request_delay_seconds,
        history_request_timeout_seconds=float(
            os.environ.get("HISTORY_REQUEST_TIMEOUT_SECONDS", "20")
        ),
        history_max_retries=int(os.environ.get("HISTORY_MAX_RETRIES", "3")),
        history_target_count=int(os.environ.get("HISTORY_TARGET_COUNT", "10000")),
        template_recommend_threshold=float(os.environ.get("TEMPLATE_RECOMMEND_THRESHOLD", "0.85")),
        ai_enabled=_parse_bool(os.environ.get("AI_ENABLED", "false")),
        openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
        openai_model=os.environ.get("OPENAI_MODEL", "gpt-5-mini"),
        openai_timeout_seconds=float(os.environ.get("OPENAI_TIMEOUT_SECONDS", "30")),
        openai_max_retries=int(os.environ.get("OPENAI_MAX_RETRIES", "2")),
    )
