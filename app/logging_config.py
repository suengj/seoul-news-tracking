"""Logging setup. Never log secret values (bot tokens, chat IDs, cookies)."""

from __future__ import annotations

import logging
import sys


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    # httpx logs each request's full URL at INFO, and the Telegram Bot API
    # embeds the bot token directly in the URL path (.../bot<TOKEN>/method)
    # — left at INFO this writes the token to every log line/file.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
