from __future__ import annotations

import httpx
import pytest

from app.database import Database
from app.telegram_bot import TelegramBotRunner, TelegramPollError
from app.telegram_sender import TelegramSender


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "bot.db")
    yield database
    database.close()


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


def _message_update(update_id, user_id, text, *, chat_id=555, message_id=None):
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id or update_id * 10,
            "chat": {"id": chat_id},
            "from": {"id": user_id, "first_name": "Test"},
            "text": text,
        },
    }


# -- authorization -----------------------------------------------------------


def test_unauthorized_user_rejected(make_settings, db):
    settings = make_settings(telegram_allowed_user_ids=(111,))
    sent = []
    bot = make_bot(settings, db, _record_handler(sent))

    outcome = bot.dispatch(_message_update(1, user_id=999, text="/status"))

    assert outcome.authorized is False
    assert len(sent) == 1
    assert (
        "권한" in sent[0]["text"] or "\\[" not in sent[0]["text"]
    )  # generic denial, not real status data
    assert "저장된 문자" not in sent[0]["text"]


def test_authorization_is_by_user_id_not_chat_id(make_settings, db):
    # An unauthorized user messaging from the "right" chat_id must still be rejected.
    settings = make_settings(telegram_allowed_user_ids=(111,))
    sent = []
    bot = make_bot(settings, db, _record_handler(sent))
    outcome = bot.dispatch(_message_update(1, user_id=999, text="/status", chat_id=42))
    assert outcome.authorized is False


# -- /latest and ordinary text ------------------------------------------------


def test_latest_command_empty_db(make_settings, db):
    settings = make_settings(telegram_allowed_user_ids=(111,))
    sent = []
    bot = make_bot(settings, db, _record_handler(sent))
    outcome = bot.dispatch(_message_update(1, user_id=111, text="/latest"))
    assert outcome.authorized is True
    assert "없습니다" in sent[0]["text"]


def test_latest_command_returns_full_record(make_settings, db, make_record):
    settings = make_settings(telegram_allowed_user_ids=(111,))
    record = make_record(source_id="LATEST1", body="긴급 대피 안내 ▲즉시 이동")
    db.insert(record)
    sent = []
    bot = make_bot(settings, db, _record_handler(sent))
    bot.dispatch(_message_update(1, user_id=111, text="/latest"))
    assert "대피 안내" in sent[0]["text"]
    assert "▲즉시" in sent[0]["text"]


def test_ordinary_authorized_text_returns_latest_record(make_settings, db, make_record):
    settings = make_settings(telegram_allowed_user_ids=(111,))
    db.insert(make_record(source_id="ORD1", body="일반 텍스트로 조회되는 문자"))
    sent = []
    bot = make_bot(settings, db, _record_handler(sent))
    bot.dispatch(_message_update(1, user_id=111, text="아무 텍스트나 보냄"))
    assert "일반 텍스트로 조회되는 문자" in sent[0]["text"]


def test_reply_threads_to_triggering_message(make_settings, db, make_record):
    settings = make_settings(telegram_allowed_user_ids=(111,))
    db.insert(make_record(source_id="R1"))
    sent = []
    bot = make_bot(settings, db, _record_handler(sent))
    bot.dispatch(_message_update(1, user_id=111, text="/latest", message_id=777))
    assert sent[0]["reply_to_message_id"] == "777"


# -- /status ------------------------------------------------------------------


def test_status_command_active(make_settings, db):
    settings = make_settings(telegram_allowed_user_ids=(111,))
    sent = []
    bot = make_bot(settings, db, _record_handler(sent))
    bot.dispatch(_message_update(1, user_id=111, text="/status"))
    assert "상태" in sent[0]["text"]


def test_status_does_not_leak_secrets_or_paths(make_settings, db, tmp_path):
    settings = make_settings(
        telegram_allowed_user_ids=(111,),
        telegram_bot_token="SUPER_SECRET_TOKEN_VALUE",
        database_path=tmp_path / "secret_dir" / "seoul_news.db",
    )
    sent = []
    bot = make_bot(settings, db, _record_handler(sent))
    bot.dispatch(_message_update(1, user_id=111, text="/status"))
    text = sent[0]["text"]
    assert "SUPER_SECRET_TOKEN_VALUE" not in text
    assert str(settings.database_path) not in text
    assert "secret_dir" not in text
    assert "TEST_CHAT" not in text


# -- /pause and /resume --------------------------------------------------------


def test_pause_command(make_settings, db):
    settings = make_settings(telegram_allowed_user_ids=(111,))
    sent = []
    bot = make_bot(settings, db, _record_handler(sent))
    bot.dispatch(_message_update(1, user_id=111, text="/pause"))
    assert db.is_polling_enabled() is False
    assert "일시정지" in sent[0]["text"]


def test_repeated_pause_is_idempotent_and_replies_accordingly(make_settings, db):
    settings = make_settings(telegram_allowed_user_ids=(111,))
    sent = []
    bot = make_bot(settings, db, _record_handler(sent))
    bot.dispatch(_message_update(1, user_id=111, text="/pause"))
    bot.dispatch(_message_update(2, user_id=111, text="/pause"))
    assert db.is_polling_enabled() is False
    assert "이미" in sent[1]["text"]


def test_resume_command(make_settings, db):
    settings = make_settings(telegram_allowed_user_ids=(111,))
    sent = []
    bot = make_bot(settings, db, _record_handler(sent))
    bot.dispatch(_message_update(1, user_id=111, text="/pause"))
    bot.dispatch(_message_update(2, user_id=111, text="/resume"))
    assert db.is_polling_enabled() is True
    assert "재개" in sent[1]["text"]


def test_repeated_resume_is_idempotent(make_settings, db):
    settings = make_settings(telegram_allowed_user_ids=(111,))
    sent = []
    bot = make_bot(settings, db, _record_handler(sent))
    bot.dispatch(_message_update(1, user_id=111, text="/resume"))  # already active
    assert db.is_polling_enabled() is True
    assert "이미" in sent[0]["text"]


def test_pause_resume_log_actor_user_id(make_settings, db):
    settings = make_settings(telegram_allowed_user_ids=(111,))
    sent = []
    bot = make_bot(settings, db, _record_handler(sent))
    bot.dispatch(_message_update(1, user_id=111, text="/pause"))
    state = db.get_system_state()
    assert state.paused_by == 111
    assert db.run_history_count() == 1  # control event logged


def test_bot_remains_responsive_while_paused(make_settings, db, make_record):
    settings = make_settings(telegram_allowed_user_ids=(111,))
    db.insert(make_record(source_id="P1"))
    sent = []
    bot = make_bot(settings, db, _record_handler(sent))
    bot.dispatch(_message_update(1, user_id=111, text="/pause"))
    bot.dispatch(_message_update(2, user_id=111, text="/latest"))
    bot.dispatch(_message_update(3, user_id=111, text="/status"))
    bot.dispatch(_message_update(4, user_id=111, text="/resume"))
    assert len(sent) == 4  # every command still got a reply


# -- /help and unknown/dev commands -------------------------------------------


def test_help_command(make_settings, db):
    settings = make_settings(telegram_allowed_user_ids=(111,))
    sent = []
    bot = make_bot(settings, db, _record_handler(sent))
    bot.dispatch(_message_update(1, user_id=111, text="/help"))
    assert "latest" in sent[0]["text"]


def test_shutdown_disabled_by_default(make_settings, db):
    settings = make_settings(telegram_allowed_user_ids=(111,), local_shutdown_command_enabled=False)
    sent = []
    bot = make_bot(settings, db, _record_handler(sent))
    bot.dispatch(_message_update(1, user_id=111, text="/shutdown"))
    assert bot._running is True
    assert "비활성화" in sent[0]["text"]


def test_shutdown_requires_flag_and_authorization(make_settings, db):
    settings = make_settings(telegram_allowed_user_ids=(111,), local_shutdown_command_enabled=True)
    sent = []
    bot = make_bot(settings, db, _record_handler(sent))
    # Unauthorized user cannot shut it down even with the flag enabled.
    bot.dispatch(_message_update(1, user_id=999, text="/shutdown"))
    assert bot._running is True

    bot.dispatch(_message_update(2, user_id=111, text="/shutdown"))
    assert bot._running is False


# -- malformed / unsupported update types -------------------------------------


@pytest.mark.parametrize(
    "update",
    [
        {
            "update_id": 1,
            "edited_message": {
                "message_id": 1,
                "chat": {"id": 1},
                "from": {"id": 111},
                "text": "x",
            },
        },
        {"update_id": 2, "channel_post": {"message_id": 1, "chat": {"id": 1}, "text": "x"}},
        {"update_id": 4},
        {
            "update_id": 5,
            "message": {"message_id": 1, "chat": {"id": 1}, "from": {"id": 111}},
        },  # no text (photo/sticker/etc.)
        {"update_id": 6, "message": {"message_id": 1, "chat": {"id": 1}, "text": "no from field"}},
        {
            "update_id": 7,
            "message": {"message_id": 1, "from": {"id": 111}, "text": "no chat field"},
        },
    ],
)
def test_malformed_and_unsupported_updates_are_ignored_safely(make_settings, db, update):
    settings = make_settings(telegram_allowed_user_ids=(111,))
    sent = []
    bot = make_bot(settings, db, _record_handler(sent))
    outcome = bot.dispatch(update)  # must not raise
    assert outcome.handled is False
    assert sent == []


def test_callback_query_updates_are_routed_and_handled(make_settings, db):
    """Unlike the truly unsupported update shapes above, a callback_query
    update IS handled by the unified bot now — routed to
    app.template_flow.dispatch_callback (which itself no-ops safely on
    malformed/unrecognized callback_data, but still answers the callback)."""
    settings = make_settings(telegram_allowed_user_ids=(111,))
    sent = []
    bot = make_bot(settings, db, _record_handler(sent))
    outcome = bot.dispatch({"update_id": 3, "callback_query": {"id": "abc", "from": {"id": 111}}})
    assert outcome.handled is True
    assert outcome.command == "callback_query"
    # answer_callback_query still fires even though the (missing) callback_data
    # was rejected as malformed.
    assert len(sent) == 1


def test_dispatch_never_raises_on_completely_malformed_update(make_settings, db):
    settings = make_settings(telegram_allowed_user_ids=(111,))
    sent = []
    bot = make_bot(settings, db, _record_handler(sent))
    # Not even a dict-shaped message.
    outcome = bot.dispatch({"update_id": 99, "message": "not-a-dict"})
    assert outcome.handled is False


# -- getUpdates offset / dedup -------------------------------------------------


def test_offset_advances_past_processed_updates(make_settings, db):
    settings = make_settings(telegram_allowed_user_ids=(111,))
    sent = []

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "getUpdates" in str(request.url):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(
                    200,
                    json={"ok": True, "result": [_message_update(5, 111, "/help")]},
                )
            return httpx.Response(200, json={"ok": True, "result": []})
        body = dict(httpx.QueryParams(request.content.decode()))
        sent.append(body)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(sent)}})

    bot = make_bot(settings, db, handler)
    updates = bot.get_updates()
    assert bot._offset == 0  # unchanged until run_forever processes them
    for update in updates:
        bot.dispatch(update)
        bot._offset = update["update_id"] + 1
    assert bot._offset == 6


def test_run_forever_does_not_reprocess_same_update(make_settings, db):
    """A getUpdates poll that would return the same update again after the
    offset has moved past it (e.g. a stale/duplicate response) must not
    cause it to be handled twice — the offset sent on the next request is
    what Telegram uses to avoid redelivering it, and the runner must use it
    correctly every iteration."""
    settings = make_settings(telegram_allowed_user_ids=(111,))
    sent = []
    seen_offsets = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "getUpdates" in str(request.url):
            offset = request.url.params.get("offset")
            seen_offsets.append(offset)
            if offset == "0":
                return httpx.Response(
                    200, json={"ok": True, "result": [_message_update(1, 111, "/help")]}
                )
            # A correctly-implemented server would never redeliver update_id=1
            # once offset=2 was requested; simulate that server contract.
            return httpx.Response(200, json={"ok": True, "result": []})
        body = dict(httpx.QueryParams(request.content.decode()))
        sent.append(body)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(sent)}})

    bot = make_bot(settings, db, handler)
    bot.run_forever(max_iterations=3)

    assert len(sent) == 1  # /help handled exactly once
    assert seen_offsets[0] == "0"
    assert seen_offsets[1] == "2"  # offset correctly advanced past update_id=1
    assert bot._offset == 2


def test_getupdates_uses_current_offset_in_request(make_settings, db):
    settings = make_settings(telegram_allowed_user_ids=(111,))
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["offset"] = request.url.params.get("offset")
        return httpx.Response(200, json={"ok": True, "result": []})

    bot = make_bot(settings, db, handler)
    bot._offset = 42
    bot.get_updates()
    assert captured["offset"] == "42"


def test_getupdates_raises_pollerror_on_409_conflict(make_settings, db):
    settings = make_settings(telegram_allowed_user_ids=(111,))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, text="Conflict: terminated by other getUpdates request")

    bot = make_bot(settings, db, handler)
    with pytest.raises(TelegramPollError):
        bot.get_updates()


def test_getupdates_raises_pollerror_on_network_failure(make_settings, db):
    settings = make_settings(telegram_allowed_user_ids=(111,))

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    bot = make_bot(settings, db, handler)
    with pytest.raises(TelegramPollError):
        bot.get_updates()
