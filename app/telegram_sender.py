"""Telegram delivery for Part 1: send the complete original message, nothing else.

No approval/rejection/editing/auto-posting — this module only knows how to
turn a DisasterMessageRecord (or an ad-hoc test string) into one or more
Telegram messages and send them, honoring TELEGRAM_SEND_ENABLED.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from app.config import Settings
from app.models import DisasterMessageRecord, TelegramStatus

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"
TELEGRAM_MESSAGE_LIMIT = 4096
MAX_SEND_RETRIES = 2
RETRY_BACKOFF_SECONDS = 2.0

# Characters MarkdownV2 requires escaping outside of intentional entities.
# https://core.telegram.org/bots/api#markdownv2-style
_MARKDOWN_V2_SPECIAL = set(r"_*[]()~`>#+-=|{}.!\\")


class TelegramError(RuntimeError):
    pass


class TelegramPermanentError(TelegramError):
    """Not worth retrying: bad token/chat id, malformed request, etc."""


class TelegramTemporaryError(TelegramError):
    """Worth a limited number of retries: network hiccup, 429, 5xx."""


@dataclass
class TelegramSendOutcome:
    status: TelegramStatus
    message_ids: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def combined_message_id(self) -> str | None:
        return ",".join(self.message_ids) if self.message_ids else None


def escape_markdown_v2(text: str) -> str:
    """Escape a dynamic value for safe inclusion in a Telegram MarkdownV2 message."""
    return "".join(("\\" + ch) if ch in _MARKDOWN_V2_SPECIAL else ch for ch in text)


def format_timestamp(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S %Z")


def build_message(record: DisasterMessageRecord) -> str:
    """Build the Part 1 Telegram message: original text preserved, dynamic fields escaped."""
    lines = [
        "\\[서울안전누리 신규 재난문자\\]",
        "",
        f"발송지역/기관: {escape_markdown_v2(record.sender_or_region)}",
        f"발송시각: {escape_markdown_v2(format_timestamp(record.sent_at))}",
        "",
        "원문:",
        escape_markdown_v2(record.original_body),
        "",
        "출처:",
        escape_markdown_v2(record.source_url),
        "",
        "수집시각:",
        escape_markdown_v2(format_timestamp(record.detected_at)),
    ]
    return "\n".join(lines)


def split_message(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Split text into <= limit chunks, preferring newline boundaries.

    Never splits immediately after a trailing backslash, which would break
    a MarkdownV2 escape sequence across chunks.
    """
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit)
        if split_at <= 0:
            split_at = limit
        while split_at > 0 and remaining[split_at - 1] == "\\":
            split_at -= 1
        if split_at <= 0:
            split_at = limit
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


class TelegramSender:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=10.0)
        )
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "TelegramSender":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def send_record(self, record: DisasterMessageRecord) -> TelegramSendOutcome:
        return self._send_text(build_message(record))

    def send_test_text(self, text: str) -> TelegramSendOutcome:
        return self._send_text(text)

    def _send_text(self, message: str) -> TelegramSendOutcome:
        if not self.settings.telegram_send_enabled:
            logger.info("TELEGRAM_SEND_ENABLED=false; skipping send")
            return TelegramSendOutcome(
                status=TelegramStatus.TELEGRAM_PENDING,
                error="send disabled (TELEGRAM_SEND_ENABLED=false)",
            )
        if not self.settings.telegram_configured:
            raise TelegramPermanentError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not configured")

        chunks = split_message(message)
        message_ids: list[str] = []
        try:
            for chunk in chunks:
                message_ids.append(self._send_chunk(chunk))
        except TelegramError as exc:
            return TelegramSendOutcome(
                status=TelegramStatus.TELEGRAM_FAILED,
                message_ids=message_ids,
                error=str(exc),
            )
        return TelegramSendOutcome(status=TelegramStatus.TELEGRAM_SENT, message_ids=message_ids)

    def _send_chunk(self, text: str) -> str:
        url = f"{TELEGRAM_API_BASE}/bot{self.settings.telegram_bot_token}/sendMessage"
        last_error: str = "unknown error"

        for attempt in range(MAX_SEND_RETRIES + 1):
            try:
                response = self._client.post(
                    url,
                    data={
                        "chat_id": self.settings.telegram_chat_id,
                        "text": text,
                        "parse_mode": "MarkdownV2",
                        "disable_web_page_preview": True,
                    },
                )
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                last_error = f"network error: {exc}"
                logger.warning("Telegram send attempt %d failed: %s", attempt + 1, last_error)
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue

            if response.status_code == 200:
                data = response.json()
                return str(data["result"]["message_id"])

            if response.status_code == 429 or response.status_code >= 500:
                last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                logger.warning("Telegram send attempt %d temporary failure: %s", attempt + 1, last_error)
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue

            # Permanent failure (e.g. 400 bad request, 401/403 bad credentials).
            raise TelegramPermanentError(f"HTTP {response.status_code}: {response.text[:200]}")

        raise TelegramTemporaryError(f"send failed after {MAX_SEND_RETRIES + 1} attempts: {last_error}")
