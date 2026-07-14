from __future__ import annotations

import re

import httpx
import pytest

from app.models import TelegramStatus
from app.telegram_sender import (
    TELEGRAM_MESSAGE_LIMIT,
    TelegramPermanentError,
    TelegramSender,
    build_confirmation_message,
    build_message,
    build_preview_complete_message,
    build_preview_incomplete_message,
    build_selection_keyboard,
    build_template_alert_message,
    escape_markdown_v2,
    make_callback_data,
    make_preview_callback_data,
    parse_callback_data,
    parse_preview_callback_data,
    split_message,
)

_MARKDOWN_V2_SPECIAL_PATTERN = re.compile(r"(?<!\\)[_*\[\]()~`>#+=|{}.!-]")


def assert_no_unescaped_markdown_v2(text: str) -> None:
    """Fail if `text` contains a MarkdownV2 reserved char not preceded by a backslash.

    Regression guard for the bug where the timestamp in a Telegram message
    (e.g. "2026-07-13 ...") was interpolated without escaping, which Telegram
    rejected with HTTP 400 "Character '-' is reserved" on a real send.
    """
    match = _MARKDOWN_V2_SPECIAL_PATTERN.search(text)
    assert match is None, (
        f"unescaped MarkdownV2 char {match.group()!r} at {match.start()} in: {text!r}"
    )


def test_escape_markdown_v2_escapes_reserved_characters():
    raw = "안내: [테스트] *강조* (참고) 100%.!"
    escaped = escape_markdown_v2(raw)
    for ch in "[]*().!":
        assert f"\\{ch}" in escaped
    # Non-special characters are untouched.
    assert "안내" in escaped


def test_build_message_contains_required_sections(make_record):
    record = make_record(body="원문 내용 ▲중요")
    message = build_message(record)
    assert "발송지역/기관:" in message
    assert "발송시각:" in message
    assert "원문:" in message
    assert "출처:" in message
    assert "수집시각:" in message
    # The original body's ▲ symbol must survive (not stripped).
    assert "▲중요" in message


def test_build_message_has_no_unescaped_markdown_v2_chars(make_record):
    # sent_at/detected_at render with dashes and colons (e.g. "2026-07-13
    # 09:00:00"); the source URL contains dots and slashes. All of it must
    # come out escaped or Telegram's real API rejects the whole message.
    record = make_record(
        body="본문 [대괄호] *별표* (괄호) 100%.",
        source_url="https://safecity.seoul.go.kr/news/dist/dust/newsDistDustList.page",
    )
    assert_no_unescaped_markdown_v2(build_message(record))


def test_split_message_returns_single_chunk_when_short():
    assert split_message("short text") == ["short text"]


def test_split_message_splits_long_text_within_limit():
    long_text = "\n".join(f"line {i} " + ("x" * 50) for i in range(200))
    chunks = split_message(long_text)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= TELEGRAM_MESSAGE_LIMIT
    # Reassembling (accounting for stripped leading newlines) preserves all lines.
    rejoined = "\n".join(chunks)
    for i in (0, 50, 199):
        assert f"line {i} " in rejoined


def test_split_message_does_not_break_escape_sequence():
    # Construct text where a naive split at `limit` would land right after a backslash.
    prefix = "a" * (TELEGRAM_MESSAGE_LIMIT - 1)
    text = prefix + "\\!" + "b" * 10
    chunks = split_message(text)
    for chunk in chunks:
        # No chunk should end with a lone trailing backslash.
        assert not chunk.endswith("\\")


def test_telegram_disabled_mode_skips_send(make_record, make_settings):
    settings = make_settings(telegram_send_enabled=False)
    sender = TelegramSender(
        settings, client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    )
    outcome = sender.send_record(make_record())
    assert outcome.status == TelegramStatus.TELEGRAM_PENDING
    assert outcome.message_ids == []
    sender.close()


def test_telegram_missing_config_raises(make_record, make_settings):
    settings = make_settings(telegram_bot_token="", telegram_chat_id="")
    sender = TelegramSender(
        settings, client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    )
    with pytest.raises(TelegramPermanentError):
        sender.send_record(make_record())
    sender.close()


def test_telegram_successful_send_stores_message_id(make_record, make_settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    settings = make_settings()
    sender = TelegramSender(settings, client=httpx.Client(transport=httpx.MockTransport(handler)))
    outcome = sender.send_record(make_record())
    assert outcome.status == TelegramStatus.TELEGRAM_SENT
    assert outcome.message_ids == ["42"]
    sender.close()


def test_telegram_permanent_failure_marks_failed(make_record, make_settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="Bad Request: chat not found")

    settings = make_settings()
    sender = TelegramSender(settings, client=httpx.Client(transport=httpx.MockTransport(handler)))
    outcome = sender.send_record(make_record())
    assert outcome.status == TelegramStatus.TELEGRAM_FAILED
    assert "400" in outcome.error
    sender.close()


def test_telegram_retries_temporary_failure_then_succeeds(make_record, monkeypatch, make_settings):
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] < 2:
            return httpx.Response(429, text="Too Many Requests")
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

    monkeypatch.setattr("app.telegram_sender.time.sleep", lambda *_: None)
    settings = make_settings()
    sender = TelegramSender(settings, client=httpx.Client(transport=httpx.MockTransport(handler)))
    outcome = sender.send_record(make_record())
    assert outcome.status == TelegramStatus.TELEGRAM_SENT
    assert calls["count"] == 2
    sender.close()


def test_telegram_does_not_retry_indefinitely(make_record, monkeypatch, make_settings):
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(500, text="Internal Server Error")

    monkeypatch.setattr("app.telegram_sender.time.sleep", lambda *_: None)
    settings = make_settings()
    sender = TelegramSender(settings, client=httpx.Client(transport=httpx.MockTransport(handler)))
    outcome = sender.send_record(make_record())
    assert outcome.status == TelegramStatus.TELEGRAM_FAILED
    # MAX_SEND_RETRIES=2 -> at most 3 attempts, not unbounded.
    assert calls["count"] == 3
    sender.close()


# --- Service v1: send_text / send_plain_text ---------------------------------


def test_send_text_ignores_send_enabled_flag(make_settings):
    """A direct reply to an inbound command must go out even with
    TELEGRAM_SEND_ENABLED=false — that flag only gates automatic alerts."""
    settings = make_settings(telegram_send_enabled=False)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    sender = TelegramSender(settings, client=httpx.Client(transport=httpx.MockTransport(handler)))
    outcome = sender.send_text("답변", chat_id=999, reply_to_message_id=5)
    assert outcome.status == TelegramStatus.TELEGRAM_SENT
    sender.close()


def test_send_plain_text_honors_send_enabled_flag(make_settings):
    settings = make_settings(telegram_send_enabled=False)
    sender = TelegramSender(
        settings, client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    )
    outcome = sender.send_plain_text("초안 문안")
    assert outcome.status == TelegramStatus.TELEGRAM_PENDING
    sender.close()


def test_send_plain_text_attaches_keyboard_only_to_last_chunk(make_settings):
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(payloads)}})

    settings = make_settings()
    sender = TelegramSender(settings, client=httpx.Client(transport=httpx.MockTransport(handler)))
    long_text = "\n".join(f"line {i} " + ("x" * 60) for i in range(100))
    keyboard = {"inline_keyboard": [[{"text": "✅ 최종 OK", "callback_data": "preview:1:confirm"}]]}
    outcome = sender.send_plain_text(long_text, reply_markup=keyboard)
    assert outcome.status == TelegramStatus.TELEGRAM_SENT
    assert len(payloads) > 1
    assert "reply_markup" not in payloads[0]
    assert "reply_markup" in payloads[-1]
    sender.close()


# --- Service v1: callback data ------------------------------------------------


def test_make_and_parse_callback_data_roundtrip():
    data = make_callback_data(42, "HEAVY_RAIN_CLEARED")
    assert len(data.encode("utf-8")) <= 64
    assert parse_callback_data(data) == (42, "HW-05")


def test_parse_callback_data_rejects_malformed():
    assert parse_callback_data("garbage") is None
    assert parse_callback_data("tpl:not-an-int:rain_clr") is None
    assert parse_callback_data("tpl:1:unknown_code") is None


def test_parse_callback_data_accepts_legacy_short_codes():
    """Buttons already in Telegram chats still use v0.1.x short codes."""
    assert parse_callback_data("tpl:42:rain_clr") == (42, "HW-05")
    assert parse_callback_data("tpl:42:flood_adv") == (42, "FL-01")
    assert parse_callback_data("tpl:42:orig") == (42, "ORIGINAL_ONLY")


def test_make_and_parse_preview_callback_data_roundtrip():
    for action in ("confirm", "cancel", "ai"):
        data = make_preview_callback_data(7, action)
        assert len(data.encode("utf-8")) <= 64
        assert parse_preview_callback_data(data) == (7, action)


def test_parse_preview_callback_data_rejects_malformed():
    assert parse_preview_callback_data("preview:1:explode") is None
    assert parse_preview_callback_data("tpl:1:confirm") is None


# --- Service v1: keyboard / message builders ---------------------------------


def test_build_selection_keyboard_is_two_stage_category_menu():
    keyboard = build_selection_keyboard(message_id=1)
    rows = keyboard["inline_keyboard"]
    all_buttons = [button for row in rows for button in row]
    assert len(all_buttons) == 5
    labels = {button["text"] for button in all_buttons}
    assert labels == {"☔ 호우", "🔥 폭염", "🌙 열대야", "🌊 홍수", "📄 원문"}
    # No button text implies an automatic recommendation.
    assert not any("추천" in label for label in labels)


def test_build_template_alert_message_shows_recommendation_only_as_secondary_line(make_record):
    from app.template_rules import TemplateSuggestion

    record = make_record(body="오늘 18시 기준 중랑천에 홍수주의보가 발령되었습니다.")
    recommended = TemplateSuggestion(template_id="FL-01", rule_score=1.0)
    text = build_template_alert_message(record, recommended)
    assert "사용할 템플릿을 선택해 주세요." in text
    assert "실험적 추천" in text
    # The secondary hint must come after the primary call-to-action line.
    assert text.index("사용할 템플릿을 선택해 주세요.") < text.index("실험적 추천")


def test_build_template_alert_message_without_recommendation(make_record):
    record = make_record(body="관련 없는 문구")
    text = build_template_alert_message(record, None)
    assert "실험적 추천" not in text


def test_build_preview_complete_message_lists_slots_and_rendered_text():
    from app.template_extractors import SlotValue

    slots = {
        "지역": SlotValue(value="서울", source="sender_or_region", evidence="서울", confidence=1.0)
    }
    text = build_preview_complete_message("HW-05", "rule", slots, "렌더된 문안")
    assert "[템플릿 초안]" in text
    assert "Rule" in text
    assert "- 지역: 서울" in text
    assert "렌더된 문안" in text


def test_build_preview_incomplete_message_never_shows_confirm_button_implied():
    text = build_preview_incomplete_message("FL-01", {}, ["하천지점"], "원문 내용")
    assert "[템플릿 작성 미완료]" in text
    assert "누락 필드:" in text
    assert "하천지점" in text
    assert "원문 내용" in text


def test_build_confirmation_message():
    text = build_confirmation_message("HW-05", "최종 문안입니다")
    assert "[최종 확정 완료]" in text
    assert "최종 문안입니다" in text
