"""Latency logging and callback-ack gating tests."""

from __future__ import annotations

import logging

import httpx

from app.telegram_routing import log_telegram_route
from app.telegram_sender import TelegramSender


def test_slow_interactive_emits_warning(caplog, make_settings):
    caplog.set_level(logging.WARNING)
    log_telegram_route(
        action="status",
        delivery_mode="interactive",
        outcome="handled",
        elapsed_ms=2500,
        slow_threshold_ms=2000,
    )
    assert any("slow Telegram interaction" in r.message for r in caplog.records)


def test_fast_interactive_no_warning(caplog, make_settings):
    caplog.set_level(logging.WARNING)
    log_telegram_route(
        action="status",
        delivery_mode="interactive",
        outcome="handled",
        elapsed_ms=100,
        slow_threshold_ms=2000,
    )
    assert not any("slow Telegram interaction" in r.message for r in caplog.records)


def test_ai_timing_not_judged_by_threshold(caplog, make_settings):
    caplog.set_level(logging.WARNING)
    log_telegram_route(
        action="preview_ai",
        delivery_mode="interactive",
        outcome="handled",
        elapsed_ms=9000,
        slow_threshold_ms=2000,
        is_ai=True,
    )
    assert not any("slow Telegram interaction" in r.message for r in caplog.records)


def test_answer_callback_query_not_gated_by_send_enabled(make_settings):
    settings = make_settings(telegram_send_enabled=False, telegram_bot_token="TOKEN")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"ok": True, "result": True})

    sender = TelegramSender(settings, client=httpx.Client(transport=httpx.MockTransport(handler)))
    sender.answer_callback_query("cbq-1")
    assert any(u.endswith("answerCallbackQuery") for u in seen)
