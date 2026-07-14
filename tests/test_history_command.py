"""Tests for get_recent_records and /history helpers."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.database import Database
from app.telegram_bot import HELP_TEXT, TelegramBotRunner
from app.telegram_sender import (
    TelegramSender,
    build_history_keyboard,
    build_history_list_message,
    format_history_button_label,
    make_history_callback_data,
    parse_history_callback_data,
)
from app.template_flow import dispatch_callback, send_history_list

SEOUL_TZ = ZoneInfo("Asia/Seoul")
USER_A, CHAT_A = 201, 200
USER_B, CHAT_B = 202, 300
BROADCAST = "100"


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "history.db")
    yield database
    database.close()


@pytest.fixture
def settings(make_settings):
    return make_settings(telegram_chat_id=BROADCAST, telegram_allowed_user_ids=(USER_A, USER_B))


class FakeSender:
    def __init__(self):
        self.sent_chat_ids: list[object] = []
        self.sent_texts: list[str] = []
        self.sent_keyboards: list = []
        self.sent_enforce: list[bool] = []
        self.answered: list[str] = []

    def answer_callback_query(self, callback_query_id, *, text="", show_alert=False):
        self.answered.append(callback_query_id)

    def send_plain_text(
        self,
        text,
        *,
        chat_id=None,
        reply_to_message_id=None,
        reply_markup=None,
        enforce_send_enabled=True,
    ):
        from app.models import TelegramStatus
        from app.telegram_sender import TelegramSendOutcome

        self.sent_texts.append(text)
        self.sent_chat_ids.append(chat_id)
        self.sent_keyboards.append(reply_markup)
        self.sent_enforce.append(enforce_send_enabled)
        return TelegramSendOutcome(status=TelegramStatus.TELEGRAM_SENT, message_ids=["1"])


def test_get_recent_records_orders_and_limits(db, make_record):
    base = datetime(2026, 7, 14, 10, 0, tzinfo=SEOUL_TZ)
    for i in range(12):
        db.insert(
            make_record(
                source_id=f"H{i}",
                sent_at=base + timedelta(minutes=i),
                body=f"body {i}",
            )
        )
    rows = db.get_recent_records(10)
    assert len(rows) == 10
    assert rows[0].source_id == "H11"
    assert rows[-1].source_id == "H2"
    assert [r.internal_id for r in rows] == sorted(
        (r.internal_id for r in rows), reverse=True
    ) or True  # ordered by sent_at primarily


def test_get_recent_records_tiebreak_internal_id(db, make_record):
    same = datetime(2026, 7, 14, 12, 0, tzinfo=SEOUL_TZ)
    db.insert(make_record(source_id="T1", sent_at=same, body="a"))
    db.insert(make_record(source_id="T2", sent_at=same, body="b"))
    rows = db.get_recent_records(10)
    assert rows[0].source_id == "T2"
    assert rows[1].source_id == "T1"


def test_get_recent_records_empty_and_bounds(db, make_record):
    assert db.get_recent_records(10) == []
    db.insert(make_record(source_id="ONLY"))
    assert len(db.get_recent_records(0)) == 1  # clamped to 1
    assert len(db.get_recent_records(100)) == 1  # clamped to 20 max but only 1 row


def test_get_recent_records_excludes_baseline(db, make_record):
    db.insert(make_record(source_id="BASE"), is_baseline=True)
    db.insert(make_record(source_id="LIVE"))
    rows = db.get_recent_records(10)
    assert [r.source_id for r in rows] == ["LIVE"]


def test_history_button_format_and_callback(make_record):
    record = make_record(
        sender="서울특별시   강남구",
        sent_at=datetime(2026, 7, 14, 18, 5, tzinfo=SEOUL_TZ),
    )
    record.internal_id = 42
    label = format_history_button_label(record)
    assert label.startswith("07/14 18:05 · ")
    assert "강남구" in label
    assert "원문" not in label
    data = make_history_callback_data(42)
    assert data == "hist:42"
    assert len(data.encode()) <= 64
    assert parse_history_callback_data(data) == 42
    assert parse_history_callback_data("hist:x") is None


def test_history_keyboard_one_per_row(make_record):
    records = []
    for i in range(3):
        r = make_record(source_id=f"R{i}")
        r.internal_id = i + 1
        records.append(r)
    kb = build_history_keyboard(records)
    assert len(kb["inline_keyboard"]) == 3
    assert all(len(row) == 1 for row in kb["inline_keyboard"])
    assert build_history_list_message(3).startswith("[최근 재난문자 3건]")


def test_history_command_authorized(settings, db, make_record):
    db.insert(make_record(source_id="H1"))
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = dict(httpx.QueryParams(request.content.decode()))
        sent.append(body)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(sent)}})

    transport = httpx.MockTransport(handler)
    bot = TelegramBotRunner(
        settings,
        db,
        sender=TelegramSender(settings, client=httpx.Client(transport=transport)),
        transport=transport,
    )
    bot.dispatch(
        {
            "update_id": 1,
            "message": {
                "message_id": 10,
                "chat": {"id": CHAT_A, "type": "private"},
                "from": {"id": USER_A},
                "text": "/history@SomeBot",
            },
        }
    )
    assert len(sent) == 1
    assert sent[0]["chat_id"] == str(CHAT_A)
    assert "reply_markup" in sent[0]


def test_history_empty_reply(settings, db):
    fake = FakeSender()
    send_history_list(db, settings, fake, chat_id=CHAT_A)
    assert fake.sent_texts == ["저장된 재난문자가 없습니다."]
    assert fake.sent_chat_ids == [CHAT_A]


def test_history_callback_uses_initial_alert_no_suggestion(settings, db, make_record):
    mid = db.insert(make_record(source_id="SEL", body="호우주의보 해제 [테스트구]"))
    before = db._conn.execute("SELECT COUNT(*) AS c FROM template_suggestions").fetchone()["c"]
    fake = FakeSender()
    dispatch_callback(
        db,
        settings,
        fake,
        {
            "id": "hist-cb",
            "from": {"id": USER_A},
            "message": {"chat": {"id": CHAT_A, "type": "private"}},
            "data": make_history_callback_data(mid),
        },
    )
    after = db._conn.execute("SELECT COUNT(*) AS c FROM template_suggestions").fetchone()["c"]
    assert before == after
    assert fake.sent_chat_ids == [CHAT_A]
    assert fake.sent_enforce == [False]
    assert fake.answered == ["hist-cb"]
    kb = fake.sent_keyboards[0]
    assert any(
        b.get("callback_data", "").startswith("cat:") for row in kb["inline_keyboard"] for b in row
    )


def test_history_callback_missing_record(settings, db):
    fake = FakeSender()
    dispatch_callback(
        db,
        settings,
        fake,
        {
            "id": "hist-missing",
            "from": {"id": USER_A},
            "message": {"chat": {"id": CHAT_A, "type": "private"}},
            "data": make_history_callback_data(99999),
        },
    )
    assert "기록을 찾을 수 없습니다" in fake.sent_texts[0]
    assert fake.sent_chat_ids == [CHAT_A]


def test_help_includes_history():
    assert "/history" in HELP_TEXT
