from __future__ import annotations

import re

import httpx
import pytest

from app.models import TelegramStatus
from app.telegram_sender import (
    TELEGRAM_MESSAGE_LIMIT,
    TelegramPermanentError,
    TelegramSender,
    build_message,
    escape_markdown_v2,
    split_message,
)
from tests.conftest import build_settings as make_settings

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


def test_telegram_disabled_mode_skips_send(make_record):
    settings = make_settings(telegram_send_enabled=False)
    sender = TelegramSender(
        settings, client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    )
    outcome = sender.send_record(make_record())
    assert outcome.status == TelegramStatus.TELEGRAM_PENDING
    assert outcome.message_ids == []
    sender.close()


def test_telegram_missing_config_raises(make_record):
    settings = make_settings(telegram_bot_token="", telegram_chat_id="")
    sender = TelegramSender(
        settings, client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    )
    with pytest.raises(TelegramPermanentError):
        sender.send_record(make_record())
    sender.close()


def test_telegram_successful_send_stores_message_id(make_record):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    settings = make_settings()
    sender = TelegramSender(settings, client=httpx.Client(transport=httpx.MockTransport(handler)))
    outcome = sender.send_record(make_record())
    assert outcome.status == TelegramStatus.TELEGRAM_SENT
    assert outcome.message_ids == ["42"]
    sender.close()


def test_telegram_permanent_failure_marks_failed(make_record):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="Bad Request: chat not found")

    settings = make_settings()
    sender = TelegramSender(settings, client=httpx.Client(transport=httpx.MockTransport(handler)))
    outcome = sender.send_record(make_record())
    assert outcome.status == TelegramStatus.TELEGRAM_FAILED
    assert "400" in outcome.error
    sender.close()


def test_telegram_retries_temporary_failure_then_succeeds(make_record, monkeypatch):
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


def test_telegram_does_not_retry_indefinitely(make_record, monkeypatch):
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
