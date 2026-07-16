"""Data model for a normalized disaster-message record."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

# All 25 Seoul autonomous districts (자치구), for reference/tests only — the
# authoritative check is the "서울특별시" / "서울특별시 " token match below, not
# this list. Kept in case a future API variant returns a bare district name
# without the "서울특별시 " prefix (see docs/mois_api_contract_confirmed.md).
SEOUL_DISTRICTS = (
    "강남구",
    "강동구",
    "강북구",
    "강서구",
    "관악구",
    "광진구",
    "구로구",
    "금천구",
    "노원구",
    "도봉구",
    "동대문구",
    "동작구",
    "마포구",
    "서대문구",
    "서초구",
    "성동구",
    "성북구",
    "송파구",
    "양천구",
    "영등포구",
    "용산구",
    "은평구",
    "종로구",
    "중구",
    "중랑구",
)

SEOUL_PREFIX = "서울특별시"


class TelegramStatus(str, Enum):
    COLLECTED = "collected"
    TELEGRAM_PENDING = "telegram_pending"
    TELEGRAM_SENT = "telegram_sent"
    TELEGRAM_FAILED = "telegram_failed"


def split_recipient_regions(rcptn_rgn_nm: str | list[str] | None) -> list[str]:
    """Split a raw RCPTN_RGN_NM value into individual trimmed region tokens.

    Handles both the confirmed MOIS shape (comma-separated string, no space
    after the comma, but a trailing space within each token, e.g.
    `"경기도 광명시 ,서울특별시 구로구 "`) and a list-of-strings shape, should one
    ever appear from another source, by splitting every element on commas
    too and stripping every resulting token.
    """
    if rcptn_rgn_nm is None:
        return []
    raw_tokens: list[str]
    if isinstance(rcptn_rgn_nm, list):
        raw_tokens = []
        for entry in rcptn_rgn_nm:
            raw_tokens.extend(str(entry).split(","))
    else:
        raw_tokens = str(rcptn_rgn_nm).split(",")
    return [token.strip() for token in raw_tokens if token.strip()]


# v0.5.0 cross-source identity: empirically confirmed (see
# docs/safekorea_html_fallback_plan.md) that MOIS's `SN` and SafeKorea's
# `bbsSn` are the SAME numeric ID for the same message — both systems query
# the same underlying 긴급재난문자 record. This makes cross-source dedup
# exact-ID-based rather than fuzzy-text-based (raw_hash does NOT match
# across sources: SafeKorea's list HTML prepends the sending org name, e.g.
# "[노원구] ...", that MOIS's MSG_CN omits).
SOURCE_NAMESPACES = ("MOIS", "SAFEKOREA")


def strip_source_namespace(source_id: str) -> str:
    """The numeric/opaque ID portion after a recognized "MOIS:"/"SAFEKOREA:"
    prefix, or `source_id` unchanged if it doesn't have one (e.g. a legacy
    non-namespaced ID)."""
    for namespace in SOURCE_NAMESPACES:
        prefix = f"{namespace}:"
        if source_id.startswith(prefix):
            return source_id[len(prefix) :]
    return source_id


def cross_source_equivalent_ids(source_id: str) -> tuple[str, ...]:
    """Every id that would refer to the same underlying message as
    `source_id` for cross-source dedup lookups: `source_id` itself (always
    first, so a non-namespaced/legacy id is still matched on its own), plus
    every namespaced variant of its numeric part."""
    numeric = strip_source_namespace(source_id)
    candidates = [source_id, *(f"{namespace}:{numeric}" for namespace in SOURCE_NAMESPACES)]
    seen: list[str] = []
    for candidate in candidates:
        if candidate not in seen:
            seen.append(candidate)
    return tuple(seen)


def is_seoul_recipient(rcptn_rgn_nm: str | list[str] | None) -> bool:
    """Authoritative Seoul-recipient determination for a MOIS/SafeKorea record.

    True only if at least one official region token is exactly "서울특별시" or
    starts with "서울특별시 " (a Seoul district). A record whose region field is
    missing/empty is schema-invalid and must never be treated as Seoul —
    callers must not guess. Message-body keyword matching is never used.
    """
    tokens = split_recipient_regions(rcptn_rgn_nm)
    if not tokens:
        return False
    return any(token == SEOUL_PREFIX or token.startswith(SEOUL_PREFIX + " ") for token in tokens)


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
