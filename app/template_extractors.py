"""Deterministic slot extraction for Excel-catalog templates.

No NLP frameworks, no inference beyond simple regex/dictionary lookups.
Every extracted value must be traceable to either the original message body,
a structured real-time record field (`sender_or_region`), or `sent_at` (only
where a template explicitly allows a timestamp fallback). A slot that cannot
be found this way is left missing — it is never guessed or invented. AI is
not part of this module at all — it only ever runs on an explicit operator
request (see `app/ai_client.py`), never as a silent fallback here.

Extraction priority per slot, in order:
1. an explicitly stated value in the original message body
2. a structured real-time record field (`sender_or_region`)
3. the record's own `sent_at`, only for slots that allow it as a fallback
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

from app.config import ENTITY_DICTIONARY_PATH


@dataclass
class SlotValue:
    value: str
    # "message_body" | "sender_or_region" | "sent_at" | "original_message" |
    # "explicit_label" | "dictionary_match" | "regex_match" | "ai"
    source: str
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
_RIVER_SUFFIX_CORE = r"[가-힣]{1,8}(?:천|강)(?!시|군|구|동|읍|면|리|도)"
_RIVER_SUFFIX_RE = re.compile(_RIVER_SUFFIX_CORE)
_RIVER_LABEL_RE = re.compile(rf"(?:하천명|대상\s?하천)\s*[:：]\s*({_RIVER_SUFFIX_CORE})")
_RIVER_KEYWORD_WINDOW = 20  # chars either side of "홍수주의보" searched for tier 2

# Common non-river words that happen to end in 천/강 (e.g. 건강 = "health",
# not a stream) — the conservative suffix regex must not let these become a
# false "final" river value even though they pass the administrative-suffix
# exclusion above.
_RIVER_FALSE_POSITIVES = {"건강"}

_TRAILING_BRACKET_RE = re.compile(r"\[([^\[\]]{1,30})\]\s*$")

_entity_dictionary_cache: dict[Path, list[str]] = {}


def _load_river_dictionary(path: Path | None = None) -> list[str]:
    """Small, human-editable helper list (config/entity_dictionary.yaml) —
    not an exhaustive source of truth, just a higher-confidence tier before
    falling back to the conservative suffix regex."""
    dictionary_path = path or ENTITY_DICTIONARY_PATH
    cached = _entity_dictionary_cache.get(dictionary_path)
    if cached is not None:
        return cached
    raw = yaml.safe_load(dictionary_path.read_text(encoding="utf-8")) or {}
    rivers = list(raw.get("rivers") or [])
    _entity_dictionary_cache[dictionary_path] = rivers
    return rivers


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
    """Priority order (see docs/template_engine.md and spec section 14):

    1. an explicit labeled field ("하천명: ...", "대상하천: ...") anywhere
       in the text — highest confidence, a human explicitly named it.
    2. a river-suffix token found within a small window around the
       "홍수주의보" keyword — narrower and safer than a global scan.
    3. a dictionary match (config/entity_dictionary.yaml) anywhere in the
       text.
    4. the conservative global suffix regex, as a last resort — gated by
       `_RIVER_FALSE_POSITIVES` so a common non-river word (e.g. 건강) can
       never silently become the "final" answer.

    AI is deliberately not a tier here — it only ever runs on an explicit
    operator request (see app/ai_client.py), never as a silent fallback.
    """
    label_match = _RIVER_LABEL_RE.search(text)
    if label_match and label_match.group(1) not in _RIVER_FALSE_POSITIVES:
        return SlotValue(
            value=label_match.group(1),
            source="explicit_label",
            evidence=label_match.group(0).strip(),
            confidence=0.95,
        )

    for keyword_match in re.finditer("홍수주의보", text):
        start = max(0, keyword_match.start() - _RIVER_KEYWORD_WINDOW)
        end = min(len(text), keyword_match.end() + _RIVER_KEYWORD_WINDOW)
        window = text[start:end]
        near_match = _RIVER_SUFFIX_RE.search(window)
        if near_match and near_match.group(0) not in _RIVER_FALSE_POSITIVES:
            return SlotValue(
                value=near_match.group(0),
                source="regex_match",
                evidence=window.strip(),
                confidence=0.75,
            )

    for river in _load_river_dictionary():
        if river in text:
            return SlotValue(
                value=river, source="dictionary_match", evidence=river, confidence=0.9
            )

    for match in _RIVER_SUFFIX_RE.finditer(text):
        if match.group(0) not in _RIVER_FALSE_POSITIVES:
            return SlotValue(
                value=match.group(0),
                source="regex_match",
                evidence=match.group(0),
                confidence=0.5,
            )

    return None


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


def _extract_flood_advisory(
    message_text: str, sender_or_region: str, sent_at: datetime
) -> ExtractionResult:
    """FL-01 — Excel slots: 기준일시, 하천지점."""
    required = ["기준일시", "하천지점"]
    slots: dict[str, SlotValue] = {}
    if (v := _extract_time_anchor(message_text)) is not None:
        slots["기준일시"] = v
    if (v := _extract_river_name(message_text)) is not None:
        slots["하천지점"] = v
    missing = _missing(required, slots)
    return ExtractionResult(
        template_id="FL-01",
        extracted_slots=slots,
        required_slots_complete=not missing,
        missing_required_slots=missing,
    )


def _extract_heavy_rain_cleared(
    message_text: str, sender_or_region: str, sent_at: datetime
) -> ExtractionResult:
    """HW-05 — Excel slots: 지역, 해제일시."""
    required = ["지역", "해제일시"]
    slots: dict[str, SlotValue] = {}
    if (v := _extract_region(sender_or_region, message_text)) is not None:
        slots["지역"] = v
    if (v := _extract_release_time(message_text)) is not None:
        slots["해제일시"] = v
    missing = _missing(required, slots)
    return ExtractionResult(
        template_id="HW-05",
        extracted_slots=slots,
        required_slots_complete=not missing,
        missing_required_slots=missing,
    )


def _extract_heavy_rain_downgraded(
    message_text: str, sender_or_region: str, sent_at: datetime
) -> ExtractionResult:
    """HW-04 — Excel slots: 기준일시, 지역."""
    required = ["기준일시", "지역"]
    slots: dict[str, SlotValue] = {}
    if (v := _extract_time_anchor(message_text)) is not None:
        slots["기준일시"] = v
    if (v := _extract_region(sender_or_region, message_text)) is not None:
        slots["지역"] = v
    missing = _missing(required, slots)
    return ExtractionResult(
        template_id="HW-04",
        extracted_slots=slots,
        required_slots_complete=not missing,
        missing_required_slots=missing,
    )


def _extract_heavy_rain_multi_level_issued(
    message_text: str, sender_or_region: str, sent_at: datetime
) -> ExtractionResult:
    """Legacy HEAVY_RAIN_MULTI_LEVEL_ISSUED — kept for old previews only."""
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
    """HT-03 — Excel slots: 발효일시, 권역수, 상향권역, 유지권역."""
    required = ["발효일시", "권역수", "상향권역", "유지권역"]
    slots: dict[str, SlotValue] = {}
    if (v := _extract_time_anchor(message_text)) is not None:
        slots["발효일시"] = v
    elif sent_at is not None:
        slots["발효일시"] = _sent_at_fallback(sent_at)
    # Region list heuristics when labels are present; otherwise leave missing.
    if (v := _extract_labeled_region_list(message_text, "상향")) is not None:
        slots["상향권역"] = v
    if (v := _extract_labeled_region_list(message_text, "유지")) is not None:
        slots["유지권역"] = v
    missing = _missing(required, slots)
    return ExtractionResult(
        template_id="HT-03",
        extracted_slots=slots,
        required_slots_complete=not missing,
        missing_required_slots=missing,
    )


def _extract_heatwave_advisory(
    message_text: str, sender_or_region: str, sent_at: datetime
) -> ExtractionResult:
    """HT-01 — Excel slots: 기준일시, 지역."""
    required = ["기준일시", "지역"]
    slots: dict[str, SlotValue] = {}
    if (v := _extract_time_anchor(message_text)) is not None:
        slots["기준일시"] = v
    if (v := _extract_region(sender_or_region, message_text)) is not None:
        slots["지역"] = v
    missing = _missing(required, slots)
    return ExtractionResult(
        template_id="HT-01",
        extracted_slots=slots,
        required_slots_complete=not missing,
        missing_required_slots=missing,
    )


def _extract_tropical_night(
    message_text: str, sender_or_region: str, sent_at: datetime
) -> ExtractionResult:
    """TN-01 — Excel slots: 발효일시, 지역."""
    required = ["발효일시", "지역"]
    slots: dict[str, SlotValue] = {}
    if (v := _extract_time_anchor(message_text)) is not None:
        slots["발효일시"] = v
    elif sent_at is not None:
        slots["발효일시"] = _sent_at_fallback(sent_at)
    if (v := _extract_region(sender_or_region, message_text)) is not None:
        slots["지역"] = v
    missing = _missing(required, slots)
    return ExtractionResult(
        template_id="TN-01",
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


def _slot_for_name(
    name: str, message_text: str, sender_or_region: str, sent_at: datetime
) -> SlotValue | None:
    """Conservative generic mapping for the remaining Excel slot names."""
    if name == "원문":
        return SlotValue(
            value=message_text,
            source="original_message",
            evidence=message_text[:80],
            confidence=1.0,
        )
    if name in {"지역", "상향권역", "유지권역"} or name.endswith("권역"):
        if name == "지역":
            return _extract_region(sender_or_region, message_text)
        if "유지" in name:
            return _extract_labeled_region_list(message_text, "유지")
        if "상향" in name:
            return _extract_labeled_region_list(message_text, "상향")
        return _extract_region(sender_or_region, message_text)
    if "하천" in name:
        return _extract_river_name(message_text)
    if "해제" in name:
        return _extract_release_time(message_text)
    if any(token in name for token in ("일시", "시각", "시간", "발표", "발효", "기준")):
        v = _extract_time_anchor(message_text)
        if v is not None:
            return v
        if name in {"발효일시", "발표일시"} and sent_at is not None:
            return _sent_at_fallback(sent_at)
        return None
    return None


def _extract_generic(
    template_id: str,
    message_text: str,
    sender_or_region: str,
    sent_at: datetime,
    *,
    required: list[str],
    optional: list[str],
) -> ExtractionResult:
    slots: dict[str, SlotValue] = {}
    for name in list(required) + list(optional):
        v = _slot_for_name(name, message_text, sender_or_region, sent_at)
        if v is not None:
            slots[name] = v
    missing = _missing(required, slots)
    return ExtractionResult(
        template_id=template_id,
        extracted_slots=slots,
        required_slots_complete=not missing,
        missing_required_slots=missing,
    )


_EXTRACTORS = {
    "FL-01": _extract_flood_advisory,
    "HW-05": _extract_heavy_rain_cleared,
    "HW-04": _extract_heavy_rain_downgraded,
    "HEAVY_RAIN_MULTI_LEVEL_ISSUED": _extract_heavy_rain_multi_level_issued,
    "HT-03": _extract_heatwave_upgraded,
    "HT-01": _extract_heatwave_advisory,
    "TN-01": _extract_tropical_night,
    "ORIGINAL_ONLY": _extract_original_only,
}


def extract_slots(
    template_id: str, message_text: str, sender_or_region: str, sent_at: datetime
) -> ExtractionResult:
    from app.template_renderer import get_template, resolve_template_id

    canonical = resolve_template_id(template_id)
    extractor = _EXTRACTORS.get(canonical)
    if extractor is not None:
        return extractor(message_text, sender_or_region, sent_at)

    template = get_template(canonical)
    if template is None:
        return ExtractionResult(
            template_id=canonical,
            extracted_slots={},
            required_slots_complete=False,
            missing_required_slots=[],
            validation_errors=[f"no extractor registered for template_id={template_id!r}"],
        )
    return _extract_generic(
        canonical,
        message_text,
        sender_or_region,
        sent_at,
        required=template.required_slots,
        optional=template.optional_slots,
    )
