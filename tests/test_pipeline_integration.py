from __future__ import annotations

from pathlib import Path

import pytest

from app.collector import CollectionResult
from app.commands import poll_once
from app.models import TelegramStatus
from app.telegram_sender import TelegramSendOutcome

APP_DIR = Path(__file__).parent.parent / "app"


class FailingRecommendSender:
    """FakeSender that records whatever text `poll_once` sends it."""

    def __init__(self, settings):
        self.settings = settings
        self.sent_texts: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def send_plain_text(self, text, reply_markup=None):
        self.sent_texts.append(text)
        return TelegramSendOutcome(status=TelegramStatus.TELEGRAM_SENT, message_ids=["1"])


@pytest.fixture
def env_setup(tmp_path, monkeypatch):
    db_path = tmp_path / "poll_once_test.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "fake-chat")
    monkeypatch.setenv("TELEGRAM_SEND_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "")
    monkeypatch.setenv("LOG_LEVEL", "ERROR")
    return db_path


def test_template_pipeline_failure_never_blocks_original_alert(env_setup, monkeypatch, make_record):
    # Establish a real baseline first (via poll_once itself) so the next
    # poll's record is treated as genuinely new, not a baseline record.
    baseline_record = make_record(source_id="DS-BASELINE", body="baseline only")

    def _fetch_baseline(**kwargs):
        return CollectionResult(
            records=[baseline_record], method="x", fetched_count=1, full_text_confirmed=True
        )

    monkeypatch.setattr(poll_once, "fetch_records", _fetch_baseline)
    monkeypatch.setattr(poll_once, "TelegramSender", FailingRecommendSender)
    assert poll_once.main(["--send"]) == 0

    record = make_record(source_id="DS-FAIL", body="폭염주의보 발효 중")

    def _fetch_one(**kwargs):
        return CollectionResult(records=[record], method="x", fetched_count=1, full_text_confirmed=True)

    monkeypatch.setattr(poll_once, "fetch_records", _fetch_one)

    def _broken_recommend(*args, **kwargs):
        raise RuntimeError("boom: rule engine exploded")

    # The rule engine is invoked from app.template_flow.build_initial_alert
    # (poll_once no longer calls it directly), and that call is wrapped in
    # its own try/except specifically so this kind of failure can never
    # reach the caller.
    monkeypatch.setattr("app.template_flow.recommend_template", _broken_recommend)

    sender_holder = {}

    def sender_factory(settings):
        sender = FailingRecommendSender(settings)
        sender_holder["sender"] = sender
        return sender

    monkeypatch.setattr(poll_once, "TelegramSender", sender_factory)

    rc = poll_once.main(["--send"])
    assert rc == 0

    sender = sender_holder["sender"]
    assert len(sender.sent_texts) == 1
    assert "폭염주의보 발효 중" in sender.sent_texts[0]
    assert "사용할 템플릿을 선택해 주세요." in sender.sent_texts[0]
    # No rule engine result -> no secondary hint line at all (rather than a
    # placeholder like the old "권장 템플릿 없음" text).
    assert "실험적 추천" not in sender.sent_texts[0]


def test_live_runtime_never_imports_historical_database_module():
    for filename in ("poll_once.py", "run_telegram_bot.py", "test_template.py"):
        source = (APP_DIR / "commands" / filename).read_text(encoding="utf-8")
        assert "history_database" not in source
        assert "history_raw" not in source
    for filename in ("template_flow.py", "telegram_bot.py", "ai_client.py"):
        source = (APP_DIR / filename).read_text(encoding="utf-8")
        assert "history_database" not in source
        assert "history_raw" not in source


def test_live_runtime_never_imports_future_trigger_rules_classifier():
    for filename in ("poll_once.py", "run_telegram_bot.py"):
        source = (APP_DIR / "commands" / filename).read_text(encoding="utf-8")
        assert "trigger_rules" not in source
        assert "app.future" not in source
    for filename in ("template_flow.py", "telegram_bot.py"):
        source = (APP_DIR / filename).read_text(encoding="utf-8")
        assert "trigger_rules" not in source
        assert "app.future" not in source
