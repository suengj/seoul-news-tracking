"""Environment-driven configuration for Part 1.

All values come from environment variables (optionally loaded from a local
`.env` via python-dotenv). Nothing here is hardcoded per-deployment secrets.
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


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_chat_id: str
    telegram_allowed_user_ids: tuple[int, ...]
    telegram_send_enabled: bool
    database_path: Path
    log_level: str
    history_database_path: Path = Path("data/history_raw.db")
    history_request_delay_seconds: float = 1.5
    history_request_timeout_seconds: float = 20.0
    history_max_retries: int = 3
    history_target_count: int = 10000
    template_recommend_threshold: float = 0.85

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)


def load_settings(env_file: Path | None = None) -> Settings:
    """Load settings from the environment, optionally loading a `.env` file first.

    `env_file` defaults to `<project_root>/.env` and is loaded if present.
    Missing `.env` is not an error — real deployments may set env vars directly.
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
        raise ValueError(
            "TELEGRAM_ALLOWED_USER_IDS must be a comma-separated list of integers"
        ) from exc

    history_request_delay_seconds = float(
        os.environ.get("HISTORY_REQUEST_DELAY_SECONDS", "1.5")
    )
    if not (HISTORY_REQUEST_DELAY_MIN <= history_request_delay_seconds <= HISTORY_REQUEST_DELAY_MAX):
        raise ValueError(
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
        history_database_path=history_database_path,
        history_request_delay_seconds=history_request_delay_seconds,
        history_request_timeout_seconds=float(
            os.environ.get("HISTORY_REQUEST_TIMEOUT_SECONDS", "20")
        ),
        history_max_retries=int(os.environ.get("HISTORY_MAX_RETRIES", "3")),
        history_target_count=int(os.environ.get("HISTORY_TARGET_COUNT", "10000")),
        template_recommend_threshold=float(os.environ.get("TEMPLATE_RECOMMEND_THRESHOLD", "0.85")),
    )
