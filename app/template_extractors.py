"""Deterministic slot extraction for the seven message templates.

No NLP frameworks, no inference beyond simple regex/dictionary lookups.
Every extracted value must be traceable to either the original message body,
a structured real-time record field (`sender_or_region`), or `sent_at` (only
where a template explicitly allows a timestamp fallback). A slot that cannot
be found this way is left missing — it is never guessed or invented.

Extraction priority per slot, in order:
1. an explicitly stated value in the original message body
2. a structured real-time record field (`sender_or_region`)
3. the record's own `sent_at`, only for slots that allow it as a fallback
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class SlotValue:
    value: str
    source: str  # "message_body" | "sender_or_region" | "sent_at" | "original_message"
    evidence: str
    confidence: float


@dataclass
class ExtractionResult:
    template_id: str
    extracted_slots: dict[str, SlotValue] = field(default_factory=dict)
    required_slots_complete: bool = False
    missing_required_slots: list[str] = field(default_factory=list)
    validation_errors: list[str] = field(default_factory=list)


# --- shared low-level extraction helpers -----------------------------------

_DATE_PREFIX = r"(?:\d{1,2}월\s*\d{1,2}일\s*)?"
# Colon form ("14:00", "14:00시") and 시/분 form ("15시", "04시 50분") kept as
# distinct alternatives (colon form tried first) so neither can partially
# match inside the other and strand a leftover character before the anchor.
_NUMERIC_TIME = r"(?:\d{1,2}:\d{2}시?|\d{1,2}시\s?\d{0,2}분?)"
_TIME_CORE = rf"(?:{_DATE_PREFIX}{_NUMERIC_TIME}|현시각|현시간)"

# Optional object particle ("을"/"를") that may sit between a time phrase and
# its anchor word (e.g. "18시30분을 기하여").
_PARTICLE = r"(?:을|를)?\s*"

# A time phrase immediately followed by one of these anchor words is treated
# as an explicit "as of <time>" statement — the anchor word is what makes
# this safe to extract (never guessed from a bare time mention elsewhere).
_TIME_ANCHOR_RE = re.compile(rf"({_TIME_CORE}){_PARTICLE}(기준|부로|현재)")

# A time phrase specifically anchored to a clearing/release statement.
_RELEASE_TIME_RE = re.compile(rf"({_TIME_CORE}){_PARTICLE}(부로|기하여).{{0,25}}?해제")

# Excludes matches immediately followed by an administrative-unit suffix
# (e.g. "예천군" -> the county name, not a river) so region names ending in
# 천/강 aren't mistaken for a river/stream name.
_RIVER_RE = re.compile(r"[가-힣]{1,8}(?:천|강)(?!시|군|구|동|읍|면|리|도)")
_TRAILING_BRACKET_RE = re.compile(r"\[([^\[\]]{1,30})\]\s*$")


def _extract_time_anchor(text: str) -> SlotValue | None:
    """Time value only (e.g. "14:00시") — templates already hardcode the
    following anchor word ("기준"/"부로") in their fixed text, so the slot
    value must not duplicate it."""
    match = _TIME_ANCHOR_RE.search(text)
    if not match:
        return None
    return SlotValue(
        value=match.group(1).strip(),
        source="message_body",
        evidence=match.group(0).strip(),
        confidence=0.8,
    )


def _extract_release_time(text: str) -> SlotValue | None:
    match = _RELEASE_TIME_RE.search(text)
    if not match:
        return None
    return SlotValue(
        value=match.group(1).strip(),
        source="message_body",
        evidence=match.group(0).strip(),
        confidence=0.8,
    )


def _extract_region(sender_or_region: str, text: str) -> SlotValue | None:
    if sender_or_region:
        return SlotValue(
            value=sender_or_region,
            source="sender_or_region",
            evidence=sender_or_region,
            confidence=1.0,
        )
    match = _TRAILING_BRACKET_RE.search(text)
    if match:
        return SlotValue(
            value=match.group(1),
            source="message_body",
            evidence=match.group(0),
            confidence=0.6,
        )
    return None


def _extract_river_name(text: str) -> SlotValue | None:
    match = _RIVER_RE.search(text)
    if not match:
        return None
    return SlotValue(
        value=match.group(0),
        source="message_body",
        evidence=match.group(0),
        confidence=0.6,
    )


def _extract_labeled_region_list(text: str, label: str) -> SlotValue | None:
    """Extract free text following an explicit label like '호우경보 : ...'."""
    pattern = re.compile(rf"{re.escape(label)}\s*[:：]\s*([^\n]+)")
    match = pattern.search(text)
    if not match:
        return None
    value = match.group(1).strip(" ,;")
    if not value:
        return None
    return SlotValue(value=value, source="message_body", evidence=match.group(0), confidence=0.7)


def _missing(required: list[str], extracted: dict[str, SlotValue]) -> list[str]:
    return [slot for slot in required if slot not in extracted]


def _sent_at_fallback(sent_at: datetime) -> SlotValue:
    """`발표일시` (announcement time) is, by definition, this message's own
    send time — using `sent_at` here is not a guess, it is the one field
    that directly answers "when was this announced". Only used when no more
    specific value is stated in the body itself."""
    formatted = sent_at.strftime("%Y-%m-%d %H:%M")
    return SlotValue(value=formatted, source="sent_at", evidence=formatted, confidence=0.5)


# --- per-template extractors -------------------------------------------------


def _extract_flood_advisory_issued(
    message_text: str, sender_or_region: str, sent_at: datetime
) -> ExtractionResult:
    required = ["기준시각", "하천명"]
    slots: dict[str, SlotValue] = {}
    if (v := _extract_time_anchor(message_text)) is not None:
        slots["기준시각"] = v
    if (v := _extract_river_name(message_text)) is not None:
        slots["하천명"] = v
    missing = _missing(required, slots)
    return ExtractionResult(
        template_id="FLOOD_ADVISORY_ISSUED",
        extracted_slots=slots,
        required_slots_complete=not missing,
        missing_required_slots=missing,
    )


def _extract_heavy_rain_cleared(
    message_text: str, sender_or_region: str, sent_at: datetime
) -> ExtractionResult:
    required = ["지역", "해제시각"]
    slots: dict[str, SlotValue] = {}
    if (v := _extract_region(sender_or_region, message_text)) is not None:
        slots["지역"] = v
    if (v := _extract_release_time(message_text)) is not None:
        slots["해제시각"] = v
    missing = _missing(required, slots)
    return ExtractionResult(
        template_id="HEAVY_RAIN_CLEARED",
        extracted_slots=slots,
        required_slots_complete=not missing,
        missing_required_slots=missing,
    )


def _extract_heavy_rain_downgraded(
    message_text: str, sender_or_region: str, sent_at: datetime
) -> ExtractionResult:
    required = ["기준일시", "지역"]
    slots: dict[str, SlotValue] = {}
    if (v := _extract_time_anchor(message_text)) is not None:
        slots["기준일시"] = v
    if (v := _extract_region(sender_or_region, message_text)) is not None:
        slots["지역"] = v
    missing = _missing(required, slots)
    return ExtractionResult(
        template_id="HEAVY_RAIN_DOWNGRADED",
        extracted_slots=slots,
        required_slots_complete=not missing,
        missing_required_slots=missing,
    )


def _extract_heavy_rain_multi_level_issued(
    message_text: str, sender_or_region: str, sent_at: datetime
) -> ExtractionResult:
    required = ["지역", "기준시각"]
    slots: dict[str, SlotValue] = {}
    if (v := _extract_region(sender_or_region, message_text)) is not None:
        slots["지역"] = v
    if (v := _extract_time_anchor(message_text)) is not None:
        slots["기준시각"] = v
    if (v := _extract_labeled_region_list(message_text, "호우경보")) is not None:
        slots["경보지역"] = v
    if (v := _extract_labeled_region_list(message_text, "호우주의보")) is not None:
        slots["주의보지역"] = v
    if (v := _extract_labeled_region_list(message_text, "홍수주의보")) is not None:
        slots["홍수지역"] = v
        slots["홍수주의보블록"] = SlotValue(
            value=f"☔ 홍수주의보\n{v.value}",
            source="message_body",
            evidence=v.evidence,
            confidence=v.confidence,
        )
    else:
        # Optional block: empty value lets the renderer omit the whole line cleanly.
        slots["홍수주의보블록"] = SlotValue(value="", source="message_body", evidence="", confidence=1.0)
    missing = _missing(required, slots)
    return ExtractionResult(
        template_id="HEAVY_RAIN_MULTI_LEVEL_ISSUED",
        extracted_slots=slots,
        required_slots_complete=not missing,
        missing_required_slots=missing,
    )


def _extract_heatwave_upgraded(
    message_text: str, sender_or_region: str, sent_at: datetime
) -> ExtractionResult:
    required = ["발표일시", "지역"]
    slots: dict[str, SlotValue] = {}
    if (v := _extract_time_anchor(message_text)) is not None:
        slots["발표일시"] = v
    elif sent_at is not None:
        slots["발표일시"] = _sent_at_fallback(sent_at)
    if (v := _extract_region(sender_or_region, message_text)) is not None:
        slots["지역"] = v
    maintained = _extract_labeled_region_list(message_text, "유지지역")
    if maintained is not None:
        slots["유지지역"] = maintained
        slots["유지지역문구"] = SlotValue(
            value=f"{maintained.value}은(는) 폭염주의보가 유지됩니다.",
            source="message_body",
            evidence=maintained.evidence,
            confidence=maintained.confidence,
        )
    else:
        slots["유지지역문구"] = SlotValue(value="", source="message_body", evidence="", confidence=1.0)
    missing = _missing(required, slots)
    return ExtractionResult(
        template_id="HEATWAVE_UPGRADED",
        extracted_slots=slots,
        required_slots_complete=not missing,
        missing_required_slots=missing,
    )


def _extract_heatwave_advisory_issued(
    message_text: str, sender_or_region: str, sent_at: datetime
) -> ExtractionResult:
    required = ["기준일시", "지역"]
    slots: dict[str, SlotValue] = {}
    if (v := _extract_time_anchor(message_text)) is not None:
        slots["기준일시"] = v
    if (v := _extract_region(sender_or_region, message_text)) is not None:
        slots["지역"] = v
    missing = _missing(required, slots)
    return ExtractionResult(
        template_id="HEATWAVE_ADVISORY_ISSUED",
        extracted_slots=slots,
        required_slots_complete=not missing,
        missing_required_slots=missing,
    )


def _extract_tropical_night_advisory_issued(
    message_text: str, sender_or_region: str, sent_at: datetime
) -> ExtractionResult:
    required = ["발표일시", "지역"]
    slots: dict[str, SlotValue] = {}
    if (v := _extract_time_anchor(message_text)) is not None:
        slots["발표일시"] = v
    elif sent_at is not None:
        slots["발표일시"] = _sent_at_fallback(sent_at)
    if (v := _extract_region(sender_or_region, message_text)) is not None:
        slots["지역"] = v
    missing = _missing(required, slots)
    return ExtractionResult(
        template_id="TROPICAL_NIGHT_ADVISORY_ISSUED",
        extracted_slots=slots,
        required_slots_complete=not missing,
        missing_required_slots=missing,
    )


def _extract_original_only(
    message_text: str, sender_or_region: str, sent_at: datetime
) -> ExtractionResult:
    slot = SlotValue(
        value=message_text, source="original_message", evidence=message_text[:80], confidence=1.0
    )
    return ExtractionResult(
        template_id="ORIGINAL_ONLY",
        extracted_slots={"원문": slot},
        required_slots_complete=True,
        missing_required_slots=[],
    )


_EXTRACTORS = {
    "FLOOD_ADVISORY_ISSUED": _extract_flood_advisory_issued,
    "HEAVY_RAIN_CLEARED": _extract_heavy_rain_cleared,
    "HEAVY_RAIN_DOWNGRADED": _extract_heavy_rain_downgraded,
    "HEAVY_RAIN_MULTI_LEVEL_ISSUED": _extract_heavy_rain_multi_level_issued,
    "HEATWAVE_UPGRADED": _extract_heatwave_upgraded,
    "HEATWAVE_ADVISORY_ISSUED": _extract_heatwave_advisory_issued,
    "TROPICAL_NIGHT_ADVISORY_ISSUED": _extract_tropical_night_advisory_issued,
    "ORIGINAL_ONLY": _extract_original_only,
}


def extract_slots(
    template_id: str, message_text: str, sender_or_region: str, sent_at: datetime
) -> ExtractionResult:
    extractor = _EXTRACTORS.get(template_id)
    if extractor is None:
        return ExtractionResult(
            template_id=template_id,
            extracted_slots={},
            required_slots_complete=False,
            missing_required_slots=[],
            validation_errors=[f"no extractor registered for template_id={template_id!r}"],
        )
    return extractor(message_text, sender_or_region, sent_at)
