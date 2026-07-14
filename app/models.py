"""Data model for a normalized disaster-message record."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class TelegramStatus(str, Enum):
    COLLECTED = "collected"
    TELEGRAM_PENDING = "telegram_pending"
    TELEGRAM_SENT = "telegram_sent"
    TELEGRAM_FAILED = "telegram_failed"


def compute_raw_hash(sender_or_region: str, sent_at: datetime, original_body: str) -> str:
    """SHA-256 fallback identity for records without (or in addition to) a stable source ID.

    Used verbatim as `raw_hash` on every record (even when a stable source_id
    exists) so dedup logic has a content-based check available too.
    """
    payload = "\x1f".join([sender_or_region, sent_at.isoformat(), original_body]).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass
class DisasterMessageRecord:
    """Normalized record for a single Seoul SafeCity 재난문자 entry.

    `original_body` must be preserved byte-for-byte from the source
    (`smsMsg`) — no summarization, spelling correction, or symbol removal.
    """

    source_id: str
    sender_or_region: str
    sent_at: datetime
    original_body: str
    source_url: str
    detected_at: datetime
    raw_payload: dict[str, Any]
    internal_id: int | None = None
    raw_hash: str = field(init=False)
    telegram_status: TelegramStatus = TelegramStatus.COLLECTED
    telegram_message_id: str | None = None

    def __post_init__(self) -> None:
        self.raw_hash = compute_raw_hash(self.sender_or_region, self.sent_at, self.original_body)
