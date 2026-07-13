"""On-demand OpenAI slot extraction — runs only when an operator explicitly
clicks "🤖 AI로 작성" (see `app.template_flow._handle_preview_ai`).

The selected template is already fixed by the human; this module extracts
that template's declared slots only. It never chooses another template,
never rewrites the fixed YAML wording, and never renders or sends anything
directly — `template_flow.py` always pushes the validated result through
`app.template_renderer.render_template` before it's shown to the operator.

Uses the OpenAI Python SDK's own structured-output helper
(`client.chat.completions.parse(response_format=<pydantic model>)`),
verified against the current official docs
(developers.openai.com/api/docs/guides/structured-outputs, redirected from
platform.openai.com) rather than a hand-rolled JSON-mode prompt. Client-level
`timeout`/`max_retries` (official constructor args) replace any custom retry
loop. The response schema models `slots` as a list of `{name, value,
evidence}` objects rather than a free-form dict, because OpenAI's strict
mode requires fixed schema properties — arbitrary per-template slot names
aren't representable as a strict dict schema.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

from openai import OpenAI
from pydantic import BaseModel

from app.config import Settings
from app.template_extractors import SlotValue

logger = logging.getLogger(__name__)

PROMPT_VERSION = "v1"


class _AISlotEntry(BaseModel):
    name: str
    value: str
    evidence: str


class _AIExtractionResponse(BaseModel):
    template_id: str
    slots: list[_AISlotEntry]
    missing_slots: list[str]


@dataclass
class AIGenerationResult:
    status: str  # "succeeded" | "validation_failed" | "api_failed"
    slots: dict[str, SlotValue] = field(default_factory=dict)
    missing_slots: list[str] = field(default_factory=list)
    error: str | None = None
    raw_response_json: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _build_messages(
    *,
    template_id: str,
    message_text: str,
    sender_or_region: str,
    sent_at: datetime,
    required_slots: list[str],
    optional_slots: list[str],
    existing_rule_slots: dict[str, SlotValue],
) -> list[dict]:
    hints = "\n".join(f"- {name}: {slot.value}" for name, slot in existing_rule_slots.items()) or "(없음)"
    system = (
        "You extract structured field values for a fixed Korean disaster-alert "
        "message template. Only use information explicitly present in the "
        "original message text. Never invent a value. Every non-null value "
        "must include a short verbatim `evidence` excerpt copied from the "
        "original message text. If a required field cannot be found, omit it "
        "from `slots` and list its name in `missing_slots` instead of guessing."
    )
    user = (
        f"template_id: {template_id}\n"
        f"required_slots: {', '.join(required_slots) or '(none)'}\n"
        f"optional_slots: {', '.join(optional_slots) or '(none)'}\n"
        f"already-extracted rule-engine hints (may be incomplete, re-verify against the text):\n"
        f"{hints}\n\n"
        f"sender_or_region: {sender_or_region}\n"
        f"sent_at: {sent_at.isoformat()}\n\n"
        f"original message text:\n{message_text}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _get_client(settings: Settings) -> OpenAI:
    return OpenAI(
        api_key=settings.openai_api_key,
        timeout=settings.openai_timeout_seconds,
        max_retries=settings.openai_max_retries,
    )


def generate_slots(
    *,
    template_id: str,
    message_text: str,
    sender_or_region: str,
    sent_at: datetime,
    required_slots: list[str],
    optional_slots: list[str],
    existing_rule_slots: dict[str, SlotValue],
    settings: Settings,
    client: OpenAI | None = None,
) -> AIGenerationResult:
    if not settings.openai_api_key:
        return AIGenerationResult(status="api_failed", error="OPENAI_API_KEY not configured")

    active_client = client or _get_client(settings)
    messages = _build_messages(
        template_id=template_id,
        message_text=message_text,
        sender_or_region=sender_or_region,
        sent_at=sent_at,
        required_slots=required_slots,
        optional_slots=optional_slots,
        existing_rule_slots=existing_rule_slots,
    )

    try:
        completion = active_client.chat.completions.parse(
            model=settings.openai_model,
            messages=messages,
            response_format=_AIExtractionResponse,
        )
    except Exception as exc:  # noqa: BLE001 - any SDK/network error is a plain "api_failed"
        logger.warning("OpenAI request failed: %s", exc)
        return AIGenerationResult(status="api_failed", error=str(exc))

    usage = getattr(completion, "usage", None)
    input_tokens = getattr(usage, "prompt_tokens", None) if usage else None
    output_tokens = getattr(usage, "completion_tokens", None) if usage else None
    raw_response_json = completion.model_dump_json() if hasattr(completion, "model_dump_json") else None

    choice = completion.choices[0]
    if getattr(choice.message, "refusal", None):
        return AIGenerationResult(
            status="api_failed",
            error=f"model refused: {choice.message.refusal}",
            raw_response_json=raw_response_json,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    parsed = choice.message.parsed
    if parsed is None:
        return AIGenerationResult(
            status="api_failed",
            error="model did not return a parsed structured response",
            raw_response_json=raw_response_json,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    return _validate_and_build(
        parsed,
        template_id=template_id,
        message_text=message_text,
        required_slots=required_slots,
        optional_slots=optional_slots,
        raw_response_json=raw_response_json,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def _validate_and_build(
    parsed: _AIExtractionResponse,
    *,
    template_id: str,
    message_text: str,
    required_slots: list[str],
    optional_slots: list[str],
    raw_response_json: str | None,
    input_tokens: int | None,
    output_tokens: int | None,
) -> AIGenerationResult:
    if parsed.template_id != template_id:
        return AIGenerationResult(
            status="validation_failed",
            error=f"model changed template_id to {parsed.template_id!r} (selected was {template_id!r})",
            raw_response_json=raw_response_json,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    declared = set(required_slots) | set(optional_slots)
    normalized_text = _normalize(message_text)
    slots: dict[str, SlotValue] = {}

    for entry in parsed.slots:
        if entry.name not in declared:
            return AIGenerationResult(
                status="validation_failed",
                error=f"model returned undeclared slot: {entry.name!r}",
                raw_response_json=raw_response_json,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        if not entry.value.strip():
            continue
        if not entry.evidence.strip():
            return AIGenerationResult(
                status="validation_failed",
                error=f"slot {entry.name!r} has a value but no evidence",
                raw_response_json=raw_response_json,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        if _normalize(entry.evidence) not in normalized_text:
            return AIGenerationResult(
                status="validation_failed",
                error=f"slot {entry.name!r} evidence not found in original message text",
                raw_response_json=raw_response_json,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        slots[entry.name] = SlotValue(
            value=entry.value, source="ai", evidence=entry.evidence, confidence=0.7
        )

    missing_required = [name for name in required_slots if name not in slots]
    if missing_required:
        return AIGenerationResult(
            status="validation_failed",
            slots=slots,
            missing_slots=missing_required,
            error=f"missing required slot(s): {', '.join(missing_required)}",
            raw_response_json=raw_response_json,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    return AIGenerationResult(
        status="succeeded",
        slots=slots,
        missing_slots=[],
        raw_response_json=raw_response_json,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
