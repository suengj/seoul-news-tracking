"""Small helpers shared by the CLI commands. Not part of the public app API."""

from __future__ import annotations

from app.models import DisasterMessageRecord

PREVIEW_LEN = 60


def record_preview(record: DisasterMessageRecord) -> str:
    body = record.original_body.replace("\r\n", " ").replace("\n", " ")
    if len(body) > PREVIEW_LEN:
        body = body[:PREVIEW_LEN] + "…"
    return f"[{record.source_id}] {record.sent_at.isoformat()} {record.sender_or_region}: {body}"
