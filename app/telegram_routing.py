"""Compact delivery-mode / latency logging helpers for Telegram routing.

Keeps Broadcast vs interactive attribution and slow-interaction warnings in
one place so every inbound path can emit the same structured INFO line.
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


def normalize_bot_command(stripped_text: str) -> str:
    """Return the slash command without @BotUsername, lowercased.

    `/history@MyBot` → `/history`. Non-command text returns the first token
    lowercased (caller decides whether to treat it as ordinary text).
    """
    if not stripped_text:
        return ""
    first = stripped_text.split()[0]
    if first.startswith("/"):
        first = first.split("@", 1)[0]
    return first.lower()


def chat_type_of(chat: dict | None) -> str:
    if not isinstance(chat, dict):
        return "unknown"
    raw = chat.get("type")
    if raw in ("private", "group", "supergroup", "channel"):
        return str(raw)
    return "unknown"


def elapsed_ms_since(start: float) -> int:
    return max(0, int((time.monotonic() - start) * 1000))


def log_telegram_route(
    *,
    action: str,
    delivery_mode: str,
    outcome: str,
    elapsed_ms: int,
    chat_type: str = "unknown",
    update_id: Any = None,
    callback_query_id: str | None = None,
    operator_user_id: int | None = None,
    routed_chat_id: Any = None,
    source_message_id: int | None = None,
    selected_template_id: str | None = None,
    slow_threshold_ms: int = 2000,
    is_ai: bool = False,
) -> None:
    """Emit one structured INFO line; WARNING when a non-AI action is slow."""
    parts = [
        f"Telegram route action={action}",
        f"delivery_mode={delivery_mode}",
        f"chat_type={chat_type}",
        f"outcome={outcome}",
        f"elapsed_ms={elapsed_ms}",
    ]
    if update_id is not None:
        parts.append(f"update_id={update_id}")
    if callback_query_id:
        parts.append(f"callback_query_id={callback_query_id}")
    if operator_user_id is not None:
        parts.append(f"operator_user_id={operator_user_id}")
    if routed_chat_id is not None:
        parts.append(f"routed_chat_id={routed_chat_id}")
    if source_message_id is not None:
        parts.append(f"source_message_id={source_message_id}")
    if selected_template_id is not None:
        parts.append(f"selected_template_id={selected_template_id}")
    logger.info(" ".join(parts))

    if is_ai:
        return
    if delivery_mode == "interactive" and elapsed_ms > slow_threshold_ms:
        logger.warning(
            "slow Telegram interaction action=%s elapsed_ms=%s threshold_ms=%s",
            action,
            elapsed_ms,
            slow_threshold_ms,
        )
