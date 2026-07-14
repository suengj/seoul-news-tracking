"""httpx logs each request's full URL at INFO, and the Telegram Bot API
embeds the bot token in that URL — configure_logging must keep httpx/httpcore
quiet enough that the token never reaches a log file."""

from __future__ import annotations

import logging

from app.logging_config import configure_logging


def test_configure_logging_suppresses_httpx_request_logging():
    logging.getLogger("httpx").setLevel(logging.INFO)
    logging.getLogger("httpcore").setLevel(logging.INFO)

    configure_logging()

    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING
