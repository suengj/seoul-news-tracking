from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.config import Settings
from app.models import DisasterMessageRecord

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SEOUL_TZ = ZoneInfo("Asia/Seoul")


@pytest.fixture(autouse=True)
def _never_load_the_real_dotenv(tmp_path, monkeypatch):
    """Safety net: redirect config.load_settings()'s default `.env` lookup to
    an empty tmp directory for every test.

    Without this, any test path that reaches `load_settings()` without an
    explicit `env_file=` (e.g. a command's `main()` calling it internally)
    would fall back to the developer's real project `.env` — which may hold
    a live Telegram bot token, a real chat id, TELEGRAM_SEND_ENABLED=true,
    and a real OPENAI_API_KEY. This fixture makes that structurally
    impossible: PROJECT_ROOT/.env can never resolve to the real file during
    a test run.
    """
    import app.config as config_module

    monkeypatch.setattr(config_module, "PROJECT_ROOT", tmp_path)


@pytest.fixture
def fixed_mois_clock(monkeypatch):
    """Pin app.mois_api._kst_today() to 2026-07-15 12:00 KST so tests whose
    fixtures embed 2026/07/14 timestamps (mois_success.json etc.) stop
    depending on the real wall clock staying within the 1-day lookback
    window used to compute crtDt. Not autouse — request it explicitly (or
    via a thin per-file autouse wrapper) since it only matters to
    MOIS-collection tests."""
    monkeypatch.setattr(
        "app.mois_api._kst_today",
        lambda: datetime(2026, 7, 15, 12, 0, 0, tzinfo=SEOUL_TZ),
    )


def build_settings(**overrides) -> Settings:
    """Construct a fully-populated Settings for tests without touching the environment."""
    defaults = dict(
        telegram_bot_token="TEST_TOKEN",
        telegram_chat_id="TEST_CHAT",
        telegram_allowed_user_ids=(111,),
        telegram_send_enabled=True,
        database_path=None,
        log_level="INFO",
        poll_interval_seconds=300,
        status_stale_after_minutes=15,
        message_retention_days=90,
        run_history_retention_days=14,
        store_successful_noop_runs=False,
        cleanup_interval_hours=24,
        tombstone_retention_days=365,
        local_shutdown_command_enabled=False,
        template_recommend_threshold=0.85,
        ai_enabled=False,
        openai_api_key="",
        openai_model="gpt-5-mini",
        openai_timeout_seconds=30.0,
        openai_max_retries=2,
        telegram_slow_interaction_ms=2000,
        telegram_ai_workers=2,
    )
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.fixture
def make_settings():
    return build_settings


@pytest.fixture
def sample_response_payload() -> dict:
    return json.loads((FIXTURES_DIR / "sample_response.json").read_text(encoding="utf-8"))


@pytest.fixture
def make_record():
    def _make(
        source_id: str = "DS00099001",
        sender: str = "서울특별시 테스트구",
        sent_at: datetime | None = None,
        body: str = "테스트 본문입니다. [테스트구]",
        source_url: str = "https://safecity.seoul.go.kr/news/dist/dust/newsDistDustList.page",
        detected_at: datetime | None = None,
    ) -> DisasterMessageRecord:
        sent_at = sent_at or datetime(2026, 7, 13, 9, 0, 0, tzinfo=SEOUL_TZ)
        detected_at = detected_at or datetime(2026, 7, 13, 9, 1, 0, tzinfo=SEOUL_TZ)
        return DisasterMessageRecord(
            source_id=source_id,
            sender_or_region=sender,
            sent_at=sent_at,
            original_body=body,
            source_url=source_url,
            detected_at=detected_at,
            raw_payload={"disstrSmsSn": source_id},
        )

    return _make
