"""Parse and validate the `selectDisstrSms.do` JSON payload into normalized records.

See docs/source_discovery.md for how the endpoint and field mapping were
confirmed. This module only trusts the field documented there as the
complete-text source (`smsMsg`); it deliberately does not accept any
alternative "preview"/truncated field as a substitute, so a source-side
regression to truncated text is treated as unusable input rather than
silently collected.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.models import DisasterMessageRecord

SEOUL_TZ = ZoneInfo("Asia/Seoul")

REQUIRED_FIELDS = ("disstrSmsSn", "disstrDate", "lctnNm", "smsMsg")


class SourceSchemaError(ValueError):
    """Raised when the source response does not match the documented schema."""


class SourceDataError(ValueError):
    """Raised when an individual record is missing required, non-empty data."""


def parse_disstr_date(raw: str) -> datetime:
    """Parse `"YYYY/MM/DD HH:MM:SS"` as a tz-aware Asia/Seoul datetime."""
    try:
        naive = datetime.strptime(raw.strip(), "%Y/%m/%d %H:%M:%S")
    except ValueError as exc:
        raise SourceDataError(f"unparseable disstrDate: {raw!r}") from exc
    return naive.replace(tzinfo=SEOUL_TZ)


def _validate_record_shape(raw: dict[str, Any]) -> None:
    missing = [key for key in REQUIRED_FIELDS if key not in raw]
    if missing:
        raise SourceDataError(f"record missing required field(s): {missing}")

    for key in ("disstrSmsSn", "disstrDate", "lctnNm"):
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            raise SourceDataError(f"record field {key!r} is empty or not a string")

    body = raw.get("smsMsg")
    if not isinstance(body, str) or not body.strip():
        raise SourceDataError("record field 'smsMsg' (complete message body) is empty")


def parse_response(payload: Any, source_url: str, detected_at: datetime) -> list[DisasterMessageRecord]:
    """Validate the top-level response shape and parse every record.

    Raises SourceSchemaError for structural problems (not a dict, missing/
    wrong-typed 'sms' key) and SourceDataError for a bad individual record —
    both are treated as hard failures by callers, never silently skipped,
    per the "log and exit non-zero rather than silently sending incomplete
    data" requirement.
    """
    if not isinstance(payload, dict):
        raise SourceSchemaError(f"expected a JSON object at top level, got {type(payload).__name__}")

    if "sms" not in payload:
        raise SourceSchemaError("response missing top-level 'sms' key")

    sms_list = payload["sms"]
    if not isinstance(sms_list, list):
        raise SourceSchemaError(f"'sms' must be a list, got {type(sms_list).__name__}")

    records: list[DisasterMessageRecord] = []
    for raw in sms_list:
        if not isinstance(raw, dict):
            raise SourceSchemaError(f"'sms' entry must be an object, got {type(raw).__name__}")

        _validate_record_shape(raw)

        sent_at = parse_disstr_date(raw["disstrDate"])
        records.append(
            DisasterMessageRecord(
                source_id=raw["disstrSmsSn"].strip(),
                sender_or_region=raw["lctnNm"].strip(),
                sent_at=sent_at,
                original_body=raw["smsMsg"],
                source_url=source_url,
                detected_at=detected_at,
                raw_payload=raw,
            )
        )

    return records
