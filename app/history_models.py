"""Data model for a single raw historical disaster-message record.

No semantic transformation happens here: fields are stored as extracted from
the source pages, verbatim. `sent_at` is a best-effort Asia/Seoul parse of
`sent_at_raw` for indexing only — `sent_at_raw` remains the source of truth.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


def compute_history_raw_hash(
    *,
    source_id: str | None,
    sender_raw: str | None,
    sent_at_raw: str | None,
    region_raw: str | None,
    body_raw: str,
) -> str:
    """Stable-ID-first hash, per docs/history_database.md.

    Prefers `source_id` (always available for this source) so the hash is
    stable even if body/region text is edited upstream between crawls;
    falls back to a content hash for sources without a stable ID.
    """
    if source_id:
        payload = f"source_id:{source_id}"
    else:
        payload = "\x1f".join(
            [sender_raw or "", sent_at_raw or "", region_raw or "", body_raw]
        )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class HistoricalRawRecord:
    source: str
    source_id: str | None
    sent_at_raw: str | None
    sent_at: datetime | None
    sender_raw: str | None
    region_raw: str | None
    title_raw: str | None
    body_raw: str
    list_url: str
    detail_url: str | None
    raw_payload: dict[str, Any]
    source_page: int
    source_position: int
    internal_id: int | None = None
    raw_hash: str = field(init=False)

    def __post_init__(self) -> None:
        self.raw_hash = compute_history_raw_hash(
            source_id=self.source_id,
            sender_raw=self.sender_raw,
            sent_at_raw=self.sent_at_raw,
            region_raw=self.region_raw,
            body_raw=self.body_raw,
        )
