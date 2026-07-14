"""Service v1 human-in-the-loop template flow: orchestrates the deterministic
rule/extraction/rendering core (`app.template_rules`/`app.template_extractors`/
`app.template_renderer`), the on-demand AI client (`app.ai_client`), and
Telegram message/keyboard building (`app.telegram_sender`) into the actual
select -> preview -> confirm/cancel/AI state machine.

Nothing here auto-picks or auto-confirms a template. A row in
`template_decisions` — the future automation ground truth — is written only
from `_handle_preview_confirm`, i.e. only from an explicit operator "최종 OK".
"""

from __future__ import annotations

import json
import logging

from app.ai_client import generate_slots
from app.config import Settings
from app.database import Database
from app.models import DisasterMessageRecord, TelegramStatus
from app.telegram_sender import (
    TelegramSender,
    build_cancel_message,
    build_confirm_failed_message,
    build_confirmation_message,
    build_confirmed_preview_keyboard,
    build_preview_complete_message,
    build_preview_incomplete_message,
    build_preview_keyboard,
    build_selection_keyboard,
    build_stale_preview_message,
    build_template_alert_message,
    parse_callback_data,
    parse_preview_callback_data,
)
from app.template_extractors import SlotValue, extract_slots
from app.template_renderer import get_template, render_template
from app.template_rules import recommend_template

logger = logging.getLogger(__name__)

AI_PROMPT_VERSION = "v1"

# A preview in one of these statuses is the one confirm/cancel/AI callbacks
# may act on. rule_preview/ai_preview are "active"; confirmed/cancelled/
# failed/superseded are all terminal — see docs/service_v1.md "latest
# preview only" policy.
_ACTIVE_PREVIEW_STATUSES = ("rule_preview", "ai_preview")


def _slots_to_json(slots: dict[str, SlotValue]) -> str:
    return json.dumps({name: vars(v) for name, v in slots.items()}, ensure_ascii=False)


def _json_to_slots(slots_json: str | None) -> dict[str, SlotValue]:
    try:
        raw = json.loads(slots_json or "{}")
    except json.JSONDecodeError:
        return {}
    return {name: SlotValue(**value) for name, value in raw.items()}


def build_initial_alert(
    record: DisasterMessageRecord, settings: Settings
) -> tuple[str, dict, object | None, list]:
    """The rule engine still runs — shown only as a secondary, non-actionable
    hint (see `build_template_alert_message`). A failure here must never
    block the original alert, so it's caught and treated as "no hint".

    Returns `(text, keyboard, recommended, candidates)` — the caller
    (`poll_once`) persists `recommended`/`candidates` into
    `template_suggestions` without having to re-run the rule engine itself.
    """
    recommended = None
    candidates: list = []
    try:
        recommended, candidates = recommend_template(
            record.original_body,
            record.sender_or_region,
            record.sent_at,
            threshold=settings.template_recommend_threshold,
        )
    except Exception:
        logger.exception(
            "rule engine failed while building initial alert for source_id=%s", record.source_id
        )
    text = build_template_alert_message(record, recommended)
    keyboard = build_selection_keyboard(record.internal_id)
    return text, keyboard, recommended, candidates


def _authorized(settings: Settings, user_id: int | None) -> bool:
    return user_id is not None and user_id in settings.telegram_allowed_user_ids


def dispatch_callback(
    db: Database, settings: Settings, sender: TelegramSender, callback_query: dict
) -> None:
    """Single entry point for every inline-button press: template selection,
    preview confirm/cancel/AI. Authorization and the duplicate-callback guard
    both happen here, before any routing."""
    callback_query_id = callback_query.get("id", "")
    # Answer immediately so Telegram stops showing the loading spinner,
    # regardless of what happens next.
    sender.answer_callback_query(callback_query_id)

    from_user = callback_query.get("from") or {}
    user_id = from_user.get("id")
    if not _authorized(settings, user_id):
        logger.warning("rejected callback from unauthorized user_id=%s", user_id)
        return

    if callback_query_id and db.has_processed_callback(callback_query_id):
        logger.info("duplicate callback_query_id=%s ignored (already processed)", callback_query_id)
        return

    data = callback_query.get("data") or ""

    tpl_parsed = parse_callback_data(data)
    if tpl_parsed is not None:
        message_id, template_id = tpl_parsed
        if callback_query_id:
            db.mark_callback_processed(callback_query_id)
        try:
            _handle_template_selection(
                db,
                settings,
                sender,
                message_id=message_id,
                template_id=template_id,
                user_id=user_id,
                callback_query_id=callback_query_id,
            )
        except Exception:
            logger.exception("template selection handling failed for message_id=%s", message_id)
        return

    preview_parsed = parse_preview_callback_data(data)
    if preview_parsed is not None:
        preview_id, action = preview_parsed
        if callback_query_id:
            db.mark_callback_processed(callback_query_id)
        try:
            if action == "confirm":
                _handle_preview_confirm(db, settings, sender, preview_id=preview_id, user_id=user_id)
            elif action == "cancel":
                _handle_preview_cancel(db, settings, sender, preview_id=preview_id, user_id=user_id)
            else:
                _handle_preview_ai(db, settings, sender, preview_id=preview_id, user_id=user_id)
        except Exception:
            logger.exception("preview action=%s handling failed for preview_id=%s", action, preview_id)
        return

    logger.warning("rejected malformed callback_data=%r", data)


def _handle_template_selection(
    db: Database,
    settings: Settings,
    sender: TelegramSender,
    *,
    message_id: int,
    template_id: str,
    user_id: int,
    callback_query_id: str,
) -> None:
    record = db.get_by_internal_id(message_id)
    if record is None:
        logger.warning("selection callback references unknown message_id=%s", message_id)
        return

    extraction = extract_slots(template_id, record.original_body, record.sender_or_region, record.sent_at)
    render_result = render_template(template_id, extraction.extracted_slots)
    extraction_json = _slots_to_json(extraction.extracted_slots)

    action_id = None
    try:
        action_id = db.insert_template_action(
            message_id=message_id,
            selected_template_id=template_id,
            selected_by=user_id,
            callback_query_id=callback_query_id,
            extraction_json=extraction_json,
            rendered_text=render_result.rendered_text,
            status="preview_created" if render_result.success else "preview_incomplete",
        )
    except Exception:
        logger.exception("failed to record template_action for message_id=%s", message_id)

    try:
        db.supersede_active_previews(message_id=message_id, selected_by=user_id)
    except Exception:
        logger.exception("failed to supersede prior active previews for message_id=%s", message_id)

    try:
        preview_id = db.insert_preview(
            message_id=message_id,
            selected_template_id=template_id,
            selected_by=user_id,
            extraction_method="rule",
            extracted_slots_json=extraction_json,
            rendered_text=render_result.rendered_text,
            missing_slots_json=json.dumps(render_result.missing_slots, ensure_ascii=False),
            status="rule_preview",
        )
    except Exception:
        logger.exception("failed to create preview for message_id=%s", message_id)
        return

    ai_enabled = settings.ai_configured
    if render_result.success:
        text = build_preview_complete_message(
            template_id, "rule", extraction.extracted_slots, render_result.rendered_text or ""
        )
        keyboard = build_preview_keyboard(preview_id, complete=True, ai_enabled=ai_enabled)
    else:
        text = build_preview_incomplete_message(
            template_id, extraction.extracted_slots, render_result.missing_slots, record.original_body
        )
        keyboard = build_preview_keyboard(preview_id, complete=False, ai_enabled=ai_enabled)

    outcome = sender.send_plain_text(text, reply_markup=keyboard)
    if outcome.status == TelegramStatus.TELEGRAM_FAILED:
        logger.error("failed to deliver preview for preview_id=%s: %s", preview_id, outcome.error)
        try:
            db.update_preview_status(preview_id, status="failed")
            if action_id is not None:
                db.update_template_action_status(action_id, status="failed", error=outcome.error)
        except Exception:
            logger.exception("failed to record delivery failure for preview_id=%s", preview_id)


def _handle_preview_confirm(
    db: Database, settings: Settings, sender: TelegramSender, *, preview_id: int, user_id: int
) -> None:
    preview = db.get_preview(preview_id)
    if preview is None:
        logger.warning("confirm callback references unknown preview_id=%s", preview_id)
        return

    if preview.status == "confirmed":
        # Idempotent: operator re-clicked OK on an already-confirmed preview
        # (not a duplicate Telegram delivery, which is already filtered out
        # upstream) — resend the same confirmation, write nothing new.
        sender.send_plain_text(
            build_confirmation_message(preview.selected_template_id, preview.rendered_text or "")
        )
        return

    if preview.status not in _ACTIVE_PREVIEW_STATUSES:
        # cancelled / superseded / failed: a newer preview exists, or this
        # one was explicitly cancelled — never resurrect it as a decision.
        logger.info(
            "confirm rejected for preview_id=%s in non-active status=%s", preview_id, preview.status
        )
        sender.send_plain_text(build_stale_preview_message())
        return

    missing_slots = json.loads(preview.missing_slots_json or "[]")
    if missing_slots or not preview.rendered_text:
        logger.warning("confirm callback for incomplete preview_id=%s", preview_id)
        return

    record = db.get_by_internal_id(preview.message_id)

    try:
        db.upsert_decision(
            message_id=preview.message_id,
            preview_id=preview_id,
            final_template_id=preview.selected_template_id,
            final_slots_json=preview.extracted_slots_json,
            final_rendered_text=preview.rendered_text,
            generation_method=preview.extraction_method,
            confirmed_by=user_id,
            source_id_snapshot=record.source_id if record is not None else None,
            sender_or_region_snapshot=record.sender_or_region if record is not None else None,
            sent_at_snapshot=record.sent_at.isoformat() if record is not None else None,
            original_body_snapshot=record.original_body if record is not None else None,
        )
    except Exception:
        # The authoritative decision was never written — do not claim
        # success. The preview is untouched (still rule_preview/ai_preview),
        # so the same 최종 OK button can be retried.
        logger.exception("failed to persist decision for preview_id=%s", preview_id)
        sender.send_plain_text(build_confirm_failed_message())
        return

    try:
        db.update_preview_status(preview_id, status="confirmed")
    except Exception:
        # The decision itself is safely written (ground truth exists) — a
        # failure here is a bookkeeping issue, not a failed confirmation.
        # Report success; a retried OK click will just re-upsert the same
        # decision and retry marking the preview confirmed.
        logger.exception(
            "decision persisted but preview status update failed for preview_id=%s", preview_id
        )

    sender.send_plain_text(build_confirmation_message(preview.selected_template_id, preview.rendered_text))


def _handle_preview_cancel(
    db: Database, settings: Settings, sender: TelegramSender, *, preview_id: int, user_id: int
) -> None:
    preview = db.get_preview(preview_id)
    if preview is None:
        logger.warning("cancel callback references unknown preview_id=%s", preview_id)
        return

    if preview.status not in _ACTIVE_PREVIEW_STATUSES:
        # Never cancel an already-confirmed preview, and never re-announce a
        # cancel/supersede/failure that already happened.
        logger.info(
            "cancel rejected for preview_id=%s in non-active status=%s", preview_id, preview.status
        )
        sender.send_plain_text(build_stale_preview_message())
        return

    try:
        db.update_preview_status(preview_id, status="cancelled")
    except Exception:
        logger.exception("failed to mark preview_id=%s cancelled", preview_id)

    record = db.get_by_internal_id(preview.message_id)
    original_body = record.original_body if record is not None else ""
    text = build_cancel_message(original_body)
    keyboard = build_selection_keyboard(preview.message_id)
    sender.send_plain_text(text, reply_markup=keyboard)


def _handle_preview_ai(
    db: Database, settings: Settings, sender: TelegramSender, *, preview_id: int, user_id: int
) -> None:
    preview = db.get_preview(preview_id)
    if preview is None:
        logger.warning("ai callback references unknown preview_id=%s", preview_id)
        return

    if preview.status not in _ACTIVE_PREVIEW_STATUSES:
        logger.info(
            "ai rejected for preview_id=%s in non-active status=%s", preview_id, preview.status
        )
        sender.send_plain_text(build_stale_preview_message())
        return

    if not settings.ai_configured:
        # The button should already be hidden when AI is disabled — this is
        # defense-in-depth only, never the primary path.
        sender.send_plain_text("AI 기능이 비활성화되어 있습니다. (AI_ENABLED=false)")
        return

    record = db.get_by_internal_id(preview.message_id)
    if record is None:
        logger.warning("ai callback references unknown message_id=%s", preview.message_id)
        return

    template = get_template(preview.selected_template_id)
    if template is None:
        logger.warning("ai callback for unknown template_id=%s", preview.selected_template_id)
        return

    existing_slots = _json_to_slots(preview.extracted_slots_json)

    ai_generation_id = None
    try:
        ai_generation_id = db.insert_ai_generation(
            message_id=preview.message_id,
            selected_template_id=preview.selected_template_id,
            requested_by=user_id,
            model=settings.openai_model,
            prompt_version=AI_PROMPT_VERSION,
            request_slots_json=preview.extracted_slots_json,
        )
    except Exception:
        logger.exception("failed to log ai_generation for preview_id=%s", preview_id)

    result = generate_slots(
        template_id=preview.selected_template_id,
        message_text=record.original_body,
        sender_or_region=record.sender_or_region,
        sent_at=record.sent_at,
        required_slots=template.required_slots,
        optional_slots=template.optional_slots,
        existing_rule_slots=existing_slots,
        settings=settings,
    )

    if ai_generation_id is not None:
        try:
            db.update_ai_generation(
                ai_generation_id,
                status=result.status,
                response_json=result.raw_response_json,
                validated_slots_json=_slots_to_json(result.slots) if result.slots else None,
                error=result.error,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
            )
        except Exception:
            logger.exception("failed to update ai_generation_id=%s", ai_generation_id)

    if result.status != "succeeded":
        text = build_preview_incomplete_message(
            preview.selected_template_id,
            result.slots or {},
            result.missing_slots or list(template.required_slots),
            record.original_body,
        )
        # Same (original) preview_id — no new preview row for a failed
        # attempt; Cancel is still available, no AI retry button.
        keyboard = build_preview_keyboard(preview_id, complete=False, ai_enabled=False)
        sender.send_plain_text(text, reply_markup=keyboard)
        return

    render_result = render_template(preview.selected_template_id, result.slots)
    try:
        ai_preview_id = db.insert_preview(
            message_id=preview.message_id,
            selected_template_id=preview.selected_template_id,
            selected_by=user_id,
            extraction_method="ai",
            extracted_slots_json=_slots_to_json(result.slots),
            rendered_text=render_result.rendered_text,
            missing_slots_json=json.dumps(render_result.missing_slots, ensure_ascii=False),
            status="ai_preview",
            ai_generation_id=ai_generation_id,
        )
    except Exception:
        logger.exception("failed to create ai preview for message_id=%s", preview.message_id)
        return

    # AI generation succeeded (we only reach here past the `!= "succeeded"`
    # early-return above) — the source Rule preview is now superseded by the
    # new AI preview. A failed AI call never reaches this line, so the
    # source Rule preview stays confirmable/cancellable in that case.
    try:
        db.update_preview_status(preview_id, status="superseded")
    except Exception:
        logger.exception("failed to supersede source preview_id=%s after AI success", preview_id)

    if render_result.success:
        text = build_preview_complete_message(
            preview.selected_template_id, "ai", result.slots, render_result.rendered_text or ""
        )
        # Section 11: an AI preview offers only 최종 OK / 취소 — AI never
        # re-offers itself.
        keyboard = build_confirmed_preview_keyboard(ai_preview_id)
    else:
        text = build_preview_incomplete_message(
            preview.selected_template_id, result.slots, render_result.missing_slots, record.original_body
        )
        keyboard = build_preview_keyboard(ai_preview_id, complete=False, ai_enabled=False)

    sender.send_plain_text(text, reply_markup=keyboard)
