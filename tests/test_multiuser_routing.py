"""Cross-chat / multi-user regression suite for the 0.1.1 routing fix.

Fixed chat ids used throughout, matching the fix's own spec:
- configured broadcast chat: 100
- authorized User A: user_id=201, chat_id=200
- authorized User B: user_id=202, chat_id=300
- unauthorized User C: user_id=203, chat_id=400

Covers the numbered scenarios from the routing/privacy fix: automatic
broadcast isolation, per-chat interactive replies (message and callback
alike), preview ownership (chat + user), persisted getUpdates offset across
a restart, and TELEGRAM_SEND_ENABLED scoping to automatic sends only.
"""

from __future__ import annotations

import httpx
import pytest

from app.database import Database
from app.models import TelegramStatus
from app.telegram_bot import TelegramBotRunner
from app.telegram_sender import (
    TelegramSendOutcome,
    TelegramSender,
    make_callback_data,
    make_preview_callback_data,
)
from app.template_flow import dispatch_callback, send_initial_alert

BROADCAST_CHAT = "100"
USER_A_ID, USER_A_CHAT = 201, 200
USER_B_ID, USER_B_CHAT = 202, 300
USER_C_ID, USER_C_CHAT = 203, 400


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "multiuser.db")
    yield database
    database.close()


@pytest.fixture
def settings(make_settings):
    return make_settings(
        telegram_chat_id=BROADCAST_CHAT, telegram_allowed_user_ids=(USER_A_ID, USER_B_ID)
    )


def make_bot(settings, db, handler):
    transport = httpx.MockTransport(handler)
    sender = TelegramSender(settings, client=httpx.Client(transport=transport))
    return TelegramBotRunner(settings, db, sender=sender, transport=transport)


def _record_handler(sent: list[dict]):
    def handler(request: httpx.Request) -> httpx.Response:
        body = dict(httpx.QueryParams(request.content.decode()))
        sent.append(body)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(sent)}})

    return handler


def _message_update(update_id, user_id, text, *, chat_id, message_id=None):
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id or update_id * 10,
            "chat": {"id": chat_id},
            "from": {"id": user_id},
            "text": text,
        },
    }


class FakeSender:
    """Records every field a caller passed, per the fix's own test requirement."""

    def __init__(self, fail: bool = False, settings=None):
        self.sent_texts: list[str] = []
        self.sent_chat_ids: list[object] = []
        self.sent_reply_to_message_ids: list[object] = []
        self.sent_keyboards: list[dict | None] = []
        self.sent_enforce_send_enabled: list[bool] = []
        self.answered: list[str] = []
        self.fail = fail
        self.settings = settings

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
        if self.fail:
            return TelegramSendOutcome(status=TelegramStatus.TELEGRAM_FAILED, error="boom")
        if (
            enforce_send_enabled
            and self.settings is not None
            and not self.settings.telegram_send_enabled
        ):
            return TelegramSendOutcome(status=TelegramStatus.TELEGRAM_PENDING)
        self.sent_texts.append(text)
        self.sent_chat_ids.append(chat_id)
        self.sent_reply_to_message_ids.append(reply_to_message_id)
        self.sent_keyboards.append(reply_markup)
        self.sent_enforce_send_enabled.append(enforce_send_enabled)
        return TelegramSendOutcome(status=TelegramStatus.TELEGRAM_SENT, message_ids=["1"])


def _tpl_callback(message_id, template_id, *, user_id, chat_id, cbq_id="cbq1") -> dict:
    return {
        "id": cbq_id,
        "from": {"id": user_id},
        "message": {"chat": {"id": chat_id}},
        "data": make_callback_data(message_id, template_id),
    }


def _preview_callback(preview_id, action, *, user_id, chat_id, cbq_id="cbq-p1") -> dict:
    return {
        "id": cbq_id,
        "from": {"id": user_id},
        "message": {"chat": {"id": chat_id}},
        "data": make_preview_callback_data(preview_id, action),
    }


# 1. Automatic new SafeCity alert -> sent only to the configured broadcast chat.


def test_automatic_broadcast_sent_only_to_configured_chat(db, settings, make_record):
    sender = FakeSender()
    record = make_record(source_id="AUTO1", body="긴급 대피 안내")
    db.insert(record)
    send_initial_alert(
        sender,
        db,
        settings,
        record,
        target_chat_id=settings.telegram_chat_id,
        persist_suggestion=True,
        enforce_send_enabled=True,
    )
    assert sender.sent_chat_ids == [BROADCAST_CHAT]


# 2. User A /latest from chat 200 -> only chat 200.


def test_user_a_latest_routes_only_to_user_a_chat(db, settings, make_record):
    db.insert(make_record(source_id="LATEST-A", body="사용자 A 조회"))
    sent = []
    bot = make_bot(settings, db, _record_handler(sent))
    bot.dispatch(_message_update(1, USER_A_ID, "/latest", chat_id=USER_A_CHAT))

    assert len(sent) == 1
    assert sent[0]["chat_id"] == str(USER_A_CHAT)
    assert sent[0]["chat_id"] != BROADCAST_CHAT
    assert sent[0]["chat_id"] != str(USER_B_CHAT)


# 3. User B ordinary text from chat 300 -> only chat 300.


def test_user_b_ordinary_text_routes_only_to_user_b_chat(db, settings, make_record):
    db.insert(make_record(source_id="TEXT-B", body="사용자 B 조회"))
    sent = []
    bot = make_bot(settings, db, _record_handler(sent))
    bot.dispatch(_message_update(1, USER_B_ID, "아무 텍스트", chat_id=USER_B_CHAT))

    assert len(sent) == 1
    assert sent[0]["chat_id"] == str(USER_B_CHAT)
    assert sent[0]["chat_id"] != BROADCAST_CHAT
    assert sent[0]["chat_id"] != str(USER_A_CHAT)


# 4-7. User B's full callback flow (select/cancel/confirm/AI) stays in chat 300.


def test_user_b_template_selection_preview_routes_to_user_b_chat(db, settings, make_record):
    message_id = db.insert(make_record(source_id="CB-B1", body="호우주의보 해제 [테스트구]"))
    sender = FakeSender()
    dispatch_callback(
        db,
        settings,
        sender,
        _tpl_callback(message_id, "HEAVY_RAIN_CLEARED", user_id=USER_B_ID, chat_id=USER_B_CHAT),
    )
    assert sender.sent_chat_ids == [USER_B_CHAT]


def test_user_b_cancel_routes_to_user_b_chat(db, settings, make_record):
    message_id = db.insert(make_record(source_id="CB-B2", body="호우주의보 해제 [테스트구]"))
    sender = FakeSender()
    dispatch_callback(
        db,
        settings,
        sender,
        _tpl_callback(message_id, "HEAVY_RAIN_CLEARED", user_id=USER_B_ID, chat_id=USER_B_CHAT),
    )
    preview_id = db._conn.execute("SELECT preview_id FROM template_previews").fetchone()[
        "preview_id"
    ]

    dispatch_callback(
        db,
        settings,
        sender,
        _preview_callback(preview_id, "cancel", user_id=USER_B_ID, chat_id=USER_B_CHAT),
    )
    assert sender.sent_chat_ids == [USER_B_CHAT, USER_B_CHAT]
    assert "[선택 취소]" in sender.sent_texts[-1]


def test_user_b_confirm_routes_to_user_b_chat(db, settings, make_record):
    message_id = db.insert(
        make_record(
            source_id="CB-B3",
            body="오늘 15시 부로 관내에 발효 중이던 호우주의보가 해제되었습니다. [예천군]",
        )
    )
    sender = FakeSender()
    dispatch_callback(
        db,
        settings,
        sender,
        _tpl_callback(message_id, "HEAVY_RAIN_CLEARED", user_id=USER_B_ID, chat_id=USER_B_CHAT),
    )
    preview_id = db._conn.execute("SELECT preview_id FROM template_previews").fetchone()[
        "preview_id"
    ]

    dispatch_callback(
        db,
        settings,
        sender,
        _preview_callback(preview_id, "confirm", user_id=USER_B_ID, chat_id=USER_B_CHAT),
    )
    assert sender.sent_chat_ids == [USER_B_CHAT, USER_B_CHAT]
    assert "[최종 확정 완료]" in sender.sent_texts[-1]


def test_user_b_ai_disabled_reply_routes_to_user_b_chat(db, settings, make_record):
    message_id = db.insert(make_record(source_id="CB-B4", body="호우주의보 해제 [테스트구]"))
    sender = FakeSender()
    dispatch_callback(
        db,
        settings,
        sender,
        _tpl_callback(message_id, "HEAVY_RAIN_CLEARED", user_id=USER_B_ID, chat_id=USER_B_CHAT),
    )
    preview_id = db._conn.execute("SELECT preview_id FROM template_previews").fetchone()[
        "preview_id"
    ]

    dispatch_callback(
        db,
        settings,
        sender,
        _preview_callback(preview_id, "ai", user_id=USER_B_ID, chat_id=USER_B_CHAT),
    )
    assert sender.sent_chat_ids == [USER_B_CHAT, USER_B_CHAT]
    assert "비활성화" in sender.sent_texts[-1]


# 8. User A cannot operate User B's preview from another chat.


def test_user_a_cannot_confirm_user_b_preview(db, settings, make_record):
    message_id = db.insert(make_record(source_id="CB-CROSS", body="호우주의보 해제 [테스트구]"))
    sender = FakeSender()
    dispatch_callback(
        db,
        settings,
        sender,
        _tpl_callback(message_id, "HEAVY_RAIN_CLEARED", user_id=USER_B_ID, chat_id=USER_B_CHAT),
    )
    preview_id = db._conn.execute("SELECT preview_id FROM template_previews").fetchone()[
        "preview_id"
    ]

    dispatch_callback(
        db,
        settings,
        sender,
        _preview_callback(preview_id, "confirm", user_id=USER_A_ID, chat_id=USER_A_CHAT),
    )

    assert "[사용할 수 없는 요청]" in sender.sent_texts[-1]
    assert sender.sent_chat_ids[-1] == USER_A_CHAT  # rejection goes to A, not B
    assert db._conn.execute("SELECT COUNT(*) AS c FROM template_decisions").fetchone()["c"] == 0


# 9. Unauthorized User C -> generic denial in chat 400 only, no disaster-message body.


def test_unauthorized_user_c_gets_denial_only_no_body_leak(db, settings, make_record):
    db.insert(make_record(source_id="SECRET1", body="민감한 재난문자 원문 내용"))
    sent = []
    bot = make_bot(settings, db, _record_handler(sent))
    bot.dispatch(_message_update(1, USER_C_ID, "/latest", chat_id=USER_C_CHAT))

    assert len(sent) == 1
    assert sent[0]["chat_id"] == str(USER_C_CHAT)
    assert "민감한 재난문자 원문 내용" not in sent[0]["text"]


# 10. /status, /pause, /resume, /help always reply to the originating chat.


@pytest.mark.parametrize("command", ["/status", "/pause", "/resume", "/help"])
def test_control_commands_reply_to_originating_chat(db, settings, command):
    sent = []
    bot = make_bot(settings, db, _record_handler(sent))
    bot.dispatch(_message_update(1, USER_A_ID, command, chat_id=USER_A_CHAT))
    assert len(sent) == 1
    assert sent[0]["chat_id"] == str(USER_A_CHAT)


# 11. /latest does not create another template_suggestions row.


def test_latest_does_not_create_duplicate_suggestion_row(db, settings, make_record):
    db.insert(make_record(source_id="NODUP1", body="본문"))
    assert db._conn.execute("SELECT COUNT(*) AS c FROM template_suggestions").fetchone()["c"] == 0

    sent = []
    bot = make_bot(settings, db, _record_handler(sent))
    bot.dispatch(_message_update(1, USER_A_ID, "/latest", chat_id=USER_A_CHAT))
    bot.dispatch(_message_update(2, USER_A_ID, "/latest", chat_id=USER_A_CHAT))

    assert db._conn.execute("SELECT COUNT(*) AS c FROM template_suggestions").fetchone()["c"] == 0


# 12/13. Persisted getUpdates offset survives a restart; no replay of handled updates.


def test_restarting_bot_loads_persisted_offset(db, settings):
    db.set_telegram_update_offset(6)
    bot = make_bot(settings, db, _record_handler([]))
    assert bot._offset == 6


def test_offset_persisted_after_run_forever_processes_update(db, settings):
    def handler(request: httpx.Request) -> httpx.Response:
        if "getUpdates" in str(request.url):
            offset = request.url.params.get("offset")
            if offset == "0":
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "result": [_message_update(5, USER_A_ID, "/help", chat_id=USER_A_CHAT)],
                    },
                )
            return httpx.Response(200, json={"ok": True, "result": []})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    bot = make_bot(settings, db, handler)
    bot.run_forever(max_iterations=2)

    assert db.get_telegram_update_offset() == 6

    # A fresh runner against the same DB resumes from 6, never re-requesting
    # (and thus never reprocessing) update_id=5.
    bot2 = make_bot(settings, db, handler)
    assert bot2._offset == 6


# 14. callback_query routing with missing message/chat data fails closed.


def test_callback_with_missing_chat_data_fails_closed(db, settings, make_record):
    message_id = db.insert(make_record(source_id="NOCHAT1", body="본문"))
    sender = FakeSender()
    callback = {
        "id": "cbq-nochat",
        "from": {"id": USER_A_ID},
        "data": make_callback_data(message_id, "HEAVY_RAIN_CLEARED"),
    }
    dispatch_callback(db, settings, sender, callback)

    assert sender.answered == ["cbq-nochat"]  # spinner still stops
    assert sender.sent_texts == []
    assert db._conn.execute("SELECT COUNT(*) AS c FROM template_actions").fetchone()["c"] == 0


# 15. TELEGRAM_SEND_ENABLED=false: automatic broadcast disabled, interactive replies still work.


def test_send_disabled_blocks_broadcast_but_not_interactive(db, make_settings, make_record):
    settings = make_settings(
        telegram_chat_id=BROADCAST_CHAT,
        telegram_allowed_user_ids=(USER_A_ID, USER_B_ID),
        telegram_send_enabled=False,
    )
    record = make_record(source_id="DISABLED1", body="본문")
    db.insert(record)

    fake = FakeSender(settings=settings)
    outcome = send_initial_alert(
        fake,
        db,
        settings,
        record,
        target_chat_id=settings.telegram_chat_id,
        persist_suggestion=True,
        enforce_send_enabled=True,
    )
    assert outcome.status == TelegramStatus.TELEGRAM_PENDING  # broadcast skipped

    sent = []
    bot = make_bot(settings, db, _record_handler(sent))
    bot.dispatch(_message_update(1, USER_A_ID, "/latest", chat_id=USER_A_CHAT))
    assert len(sent) == 1  # interactive reply still went out
    assert sent[0]["chat_id"] == str(USER_A_CHAT)
