from __future__ import annotations

import pytest

from app.commands.run_telegram_bot import handle_callback_query
from app.config import Settings
from app.database import Database
from app.models import TelegramStatus
from app.telegram_sender import TelegramSendOutcome, build_keyboard, make_callback_data

ALLOWED_USER_ID = 111
OTHER_USER_ID = 999


class FakeSender:
    def __init__(self, fail: bool = False):
        self.sent_texts: list[str] = []
        self.answered: list[str] = []
        self.fail = fail

    def answer_callback_query(self, callback_query_id, *, text="", show_alert=False):
        self.answered.append(callback_query_id)

    def send_plain_text(self, text, reply_markup=None):
        if self.fail:
            return TelegramSendOutcome(status=TelegramStatus.TELEGRAM_FAILED, error="boom")
        self.sent_texts.append(text)
        return TelegramSendOutcome(status=TelegramStatus.TELEGRAM_SENT, message_ids=["1"])


def _settings() -> Settings:
    return Settings(
        telegram_bot_token="fake",
        telegram_chat_id="fake",
        telegram_allowed_user_ids=(ALLOWED_USER_ID,),
        telegram_send_enabled=True,
        database_path=None,  # unused by handle_callback_query directly
        log_level="ERROR",
    )


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "rt.db")
    yield database
    database.close()


@pytest.fixture
def stored_message_id(db, make_record):
    record = make_record(
        source_id="DS1",
        body="오늘 15시 부로 관내에 발효 중이던 호우주의보가 해제되었습니다. [예천군]",
        sender="예천군",
    )
    return db.insert(record)


def _callback(message_id: int, template_id: str, *, user_id: int = ALLOWED_USER_ID, cbq_id: str = "cbq1") -> dict:
    return {
        "id": cbq_id,
        "from": {"id": user_id},
        "data": make_callback_data(message_id, template_id),
    }


def test_build_keyboard_callback_data_stays_under_telegram_limit(stored_message_id):
    keyboard = build_keyboard(stored_message_id, "HEAVY_RAIN_CLEARED")
    for row in keyboard["inline_keyboard"]:
        for button in row:
            assert len(button["callback_data"].encode("utf-8")) <= 64


def test_build_keyboard_has_five_rows_with_recommendation():
    keyboard = build_keyboard(1, "HEATWAVE_UPGRADED")
    assert len(keyboard["inline_keyboard"]) == 5
    assert keyboard["inline_keyboard"][0][0]["text"] == "✅ 권장 포맷"


def test_build_keyboard_omits_recommended_button_when_none():
    keyboard = build_keyboard(1, None)
    row1_labels = [b["text"] for b in keyboard["inline_keyboard"][0]]
    assert "✅ 권장 포맷" not in row1_labels
    assert "📄 원문" in row1_labels


def test_authorized_callback_renders_and_stores_action(db, stored_message_id):
    sender = FakeSender()
    handle_callback_query(
        _callback(stored_message_id, "HEAVY_RAIN_CLEARED"), db, _settings(), sender
    )
    assert sender.answered == ["cbq1"]
    assert len(sender.sent_texts) == 1
    assert "호우특보는" in sender.sent_texts[0]

    row = db._conn.execute("SELECT * FROM template_actions").fetchone()
    assert row["selected_template_id"] == "HEAVY_RAIN_CLEARED"
    assert row["selected_by"] == ALLOWED_USER_ID
    assert row["status"] == "sent"


def test_unauthorized_callback_is_rejected_without_sending(db, stored_message_id):
    sender = FakeSender()
    handle_callback_query(
        _callback(stored_message_id, "HEAVY_RAIN_CLEARED", user_id=OTHER_USER_ID), db, _settings(), sender
    )
    # Still acknowledged (so Telegram's spinner stops), but nothing sent or stored.
    assert sender.answered == ["cbq1"]
    assert sender.sent_texts == []
    assert db._conn.execute("SELECT COUNT(*) AS c FROM template_actions").fetchone()["c"] == 0


def test_original_only_button_resends_verbatim_without_extraction(db, stored_message_id):
    sender = FakeSender()
    handle_callback_query(_callback(stored_message_id, "ORIGINAL_ONLY"), db, _settings(), sender)
    assert sender.sent_texts == [
        "오늘 15시 부로 관내에 발효 중이던 호우주의보가 해제되었습니다. [예천군]"
    ]
    row = db._conn.execute("SELECT * FROM template_actions").fetchone()
    assert row["selected_template_id"] == "ORIGINAL_ONLY"


def test_selecting_another_template_reruns_extraction_from_original(db, stored_message_id):
    sender = FakeSender()
    # The stored message has no river-name token, so manually selecting
    # FLOOD_ADVISORY_ISSUED (requires 하천명) must re-run extraction against
    # the *original* body and come back incomplete rather than inventing one.
    handle_callback_query(
        _callback(stored_message_id, "FLOOD_ADVISORY_ISSUED"), db, _settings(), sender
    )
    assert len(sender.sent_texts) == 1
    assert "[템플릿 생성 불가]" in sender.sent_texts[0]
    assert "선택 포맷:" in sender.sent_texts[0]
    assert "누락 필드:" in sender.sent_texts[0]
    assert "하천명" in sender.sent_texts[0]
    assert "원문:" in sender.sent_texts[0]

    row = db._conn.execute("SELECT * FROM template_actions").fetchone()
    assert row["status"] == "mismatch"


def test_duplicate_callback_is_processed_only_once(db, stored_message_id):
    sender = FakeSender()
    callback = _callback(stored_message_id, "HEAVY_RAIN_CLEARED", cbq_id="dup-1")
    handle_callback_query(callback, db, _settings(), sender)
    handle_callback_query(callback, db, _settings(), sender)

    assert len(sender.sent_texts) == 1
    count = db._conn.execute("SELECT COUNT(*) AS c FROM template_actions").fetchone()["c"]
    assert count == 1


def test_missing_message_id_is_rejected_safely(db):
    sender = FakeSender()
    handle_callback_query(_callback(999999, "HEAVY_RAIN_CLEARED"), db, _settings(), sender)
    assert sender.sent_texts == []
    assert db._conn.execute("SELECT COUNT(*) AS c FROM template_actions").fetchone()["c"] == 0


def test_telegram_send_failure_does_not_corrupt_db_state(db, stored_message_id):
    sender = FakeSender(fail=True)
    handle_callback_query(_callback(stored_message_id, "HEAVY_RAIN_CLEARED"), db, _settings(), sender)
    row = db._conn.execute("SELECT * FROM template_actions").fetchone()
    assert row["status"] == "failed"
    assert row["error"] == "boom"
    # DB still perfectly queryable afterwards.
    assert db._conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"] == 1
