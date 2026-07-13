"""Long-polling Telegram bot: handles inline template-button callbacks only.

Usage:
    python -m app.commands.run_telegram_bot

Does not collect or send new disaster messages itself (that remains
`poll_once`'s job) — this process only waits for `callback_query` updates
(inline button presses) on messages `poll_once` already sent, and replies
with the selected template's rendered draft. SIGINT/SIGTERM stop the loop
after the in-flight update finishes.
"""

from __future__ import annotations

import json
import logging
import signal
import sqlite3
import sys

from app.config import Settings, load_settings
from app.database import Database, open_database
from app.logging_config import configure_logging
from app.models import TelegramStatus
from app.telegram_sender import (
    TelegramSender,
    TelegramSendOutcome,
    build_mismatch_message,
    parse_callback_data,
)
from app.template_extractors import extract_slots
from app.template_renderer import render_template

logger = logging.getLogger(__name__)

GET_UPDATES_TIMEOUT_SECONDS = 25


def _send_status(outcome: TelegramSendOutcome) -> str:
    return {
        TelegramStatus.TELEGRAM_SENT: "sent",
        TelegramStatus.TELEGRAM_PENDING: "pending",
        TelegramStatus.TELEGRAM_FAILED: "failed",
    }.get(outcome.status, "failed")


def handle_callback_query(
    callback_query: dict, db: Database, settings: Settings, sender: TelegramSender
) -> None:
    callback_query_id = callback_query.get("id", "")
    # Answer immediately so Telegram stops showing the loading spinner,
    # regardless of whether the request turns out to be valid.
    sender.answer_callback_query(callback_query_id)

    from_user = callback_query.get("from") or {}
    user_id = from_user.get("id")
    if user_id not in settings.telegram_allowed_user_ids:
        logger.warning("rejected callback from unauthorized user_id=%s", user_id)
        return

    data = callback_query.get("data") or ""
    parsed = parse_callback_data(data)
    if parsed is None:
        logger.warning("rejected malformed callback_data")
        return
    message_id, template_id = parsed

    if callback_query_id and db.has_processed_callback(callback_query_id):
        logger.info("duplicate callback_query_id=%s ignored (already processed)", callback_query_id)
        return

    record = db.get_by_internal_id(message_id)
    if record is None:
        logger.warning("callback references unknown message_id=%s", message_id)
        return

    if template_id == "ORIGINAL_ONLY":
        extraction = extract_slots("ORIGINAL_ONLY", record.original_body, record.sender_or_region, record.sent_at)
        outcome = sender.send_plain_text(record.original_body)
        _record_action(
            db,
            message_id=message_id,
            template_id=template_id,
            user_id=user_id,
            callback_query_id=callback_query_id,
            extraction_json=json.dumps(
                {k: vars(v) for k, v in extraction.extracted_slots.items()}, ensure_ascii=False
            ),
            rendered_text=record.original_body,
            status=_send_status(outcome),
            error=outcome.error,
        )
        return

    extraction = extract_slots(template_id, record.original_body, record.sender_or_region, record.sent_at)
    render_result = render_template(template_id, extraction.extracted_slots)
    extraction_json = json.dumps(
        {k: vars(v) for k, v in extraction.extracted_slots.items()}, ensure_ascii=False
    )

    if render_result.success:
        outcome = sender.send_plain_text(render_result.rendered_text or "")
        _record_action(
            db,
            message_id=message_id,
            template_id=template_id,
            user_id=user_id,
            callback_query_id=callback_query_id,
            extraction_json=extraction_json,
            rendered_text=render_result.rendered_text,
            status=_send_status(outcome),
            error=outcome.error,
        )
    else:
        mismatch_text = build_mismatch_message(
            template_id, render_result.missing_slots, extraction.extracted_slots, record.original_body
        )
        outcome = sender.send_plain_text(mismatch_text)
        _record_action(
            db,
            message_id=message_id,
            template_id=template_id,
            user_id=user_id,
            callback_query_id=callback_query_id,
            extraction_json=extraction_json,
            rendered_text=mismatch_text,
            status="mismatch",
            error="; ".join(render_result.validation_errors),
        )


def _record_action(
    db: Database,
    *,
    message_id: int,
    template_id: str,
    user_id: int,
    callback_query_id: str,
    extraction_json: str,
    rendered_text: str | None,
    status: str,
    error: str | None,
) -> None:
    try:
        db.insert_template_action(
            message_id=message_id,
            selected_template_id=template_id,
            selected_by=user_id,
            callback_query_id=callback_query_id,
            extraction_json=extraction_json,
            rendered_text=rendered_text,
            status=status,
            error=error,
        )
    except sqlite3.IntegrityError:
        # Same callback_query_id already recorded — a duplicate Telegram
        # delivery of the same button press. The reply was already sent
        # above by the first delivery; nothing further to do.
        logger.info("duplicate callback_query_id=%s ignored", callback_query_id)


def main(argv: list[str] | None = None) -> int:
    settings = load_settings()
    configure_logging(settings.log_level)

    if not settings.telegram_configured:
        print("FAILED: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not configured", file=sys.stderr)
        return 1

    stop_requested = {"flag": False}

    def _handle_signal(signum: int, _frame: object) -> None:
        logger.warning("received signal %s; stopping after the current update batch...", signum)
        stop_requested["flag"] = True

    previous_sigint = signal.signal(signal.SIGINT, _handle_signal)
    previous_sigterm = signal.signal(signal.SIGTERM, _handle_signal)

    offset: int | None = None
    try:
        with open_database(settings.database_path) as db, TelegramSender(settings) as sender:
            print("run_telegram_bot: listening for inline button callbacks (Ctrl+C to stop)")
            while not stop_requested["flag"]:
                try:
                    updates = sender.get_updates(offset=offset, timeout=GET_UPDATES_TIMEOUT_SECONDS)
                except Exception as exc:  # noqa: BLE001 - keep polling past transient errors
                    logger.error("getUpdates failed: %s", exc)
                    continue
                for update in updates:
                    offset = update["update_id"] + 1
                    callback_query = update.get("callback_query")
                    if callback_query is not None:
                        handle_callback_query(callback_query, db, settings, sender)
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
        signal.signal(signal.SIGTERM, previous_sigterm)

    print("run_telegram_bot: stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
