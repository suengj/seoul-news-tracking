"""Run a single collection + dedup (+ optional Telegram send) cycle.

Usage:
    python -m app.commands.poll_once --dry-run
    python -m app.commands.poll_once --send
    python -m app.commands.poll_once --send --notify-existing

--dry-run performs no database writes and sends nothing; it only reports
what a real run would do.

Without --dry-run, newly-detected records are always stored. Telegram
sending only happens with --send, and only for genuinely new records
(plus any previously collected-but-unsent records, to support retry)
unless the database was empty at the start of this run (i.e. this run is
itself acting as the baseline) — in that baseline case nothing is sent
unless --notify-existing is also given, per the "don't notify on first
run" requirement.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from app.collector import CollectorError, EmptyWidgetError, fetch_records
from app.commands._shared import record_preview
from app.config import Settings, load_settings
from app.database import Database, open_database
from app.logging_config import configure_logging
from app.models import DisasterMessageRecord, TelegramStatus
from app.telegram_sender import (
    TelegramSender,
    TelegramSendOutcome,
    build_keyboard,
    build_template_alert_message,
)
from app.template_extractors import extract_slots
from app.template_renderer import render_template
from app.template_rules import recommend_template

logger = logging.getLogger(__name__)


def _send_via_template_pipeline(
    sender: TelegramSender, db: Database, settings: Settings, record: DisasterMessageRecord
) -> TelegramSendOutcome:
    """Suggest/extract/render for `record`, then send one Telegram alert with
    inline template buttons. Any pipeline failure falls back to a plain
    "no recommendation" alert — it never blocks delivery of the original
    message."""
    recommended = None
    candidates = []
    render_result = None
    extracted_slots: dict = {}
    try:
        recommended, candidates = recommend_template(
            record.original_body,
            record.sender_or_region,
            record.sent_at,
            threshold=settings.template_recommend_threshold,
        )
        if recommended is not None:
            extraction = extract_slots(
                recommended.template_id, record.original_body, record.sender_or_region, record.sent_at
            )
            extracted_slots = extraction.extracted_slots
            render_result = render_template(recommended.template_id, extracted_slots)
    except Exception:
        logger.exception(
            "template pipeline failed for source_id=%s; sending original with no recommendation",
            record.source_id,
        )
        recommended = None
        render_result = None
        extracted_slots = {}

    message_text = build_template_alert_message(record, recommended, render_result)
    keyboard = build_keyboard(record.internal_id, recommended.template_id if recommended else None)
    outcome = sender.send_plain_text(message_text, reply_markup=keyboard)

    db.insert_template_suggestion(
        message_id=record.internal_id,
        recommended_template_id=recommended.template_id if recommended else None,
        rule_score=recommended.rule_score if recommended else None,
        candidates_json=json.dumps([vars(c) for c in candidates], ensure_ascii=False),
        extraction_json=json.dumps({k: vars(v) for k, v in extracted_slots.items()}, ensure_ascii=False),
        rendered_text=render_result.rendered_text if render_result and render_result.success else None,
    )
    return outcome


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report only, no DB writes, no sends")
    parser.add_argument("--send", action="store_true", help="attempt Telegram delivery for new records")
    parser.add_argument(
        "--notify-existing",
        action="store_true",
        help="also send baseline records when this run establishes the baseline (testing only)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = load_settings()
    configure_logging(settings.log_level)

    try:
        result = fetch_records()
    except EmptyWidgetError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    except CollectorError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1

    print(f"selected method     : {result.method}")
    print(f"records fetched      : {result.fetched_count}")
    print(f"full text confirmed  : {result.full_text_confirmed}")

    with open_database(settings.database_path) as db:
        was_empty = db.is_empty

        new_records: list[DisasterMessageRecord] = []
        duplicate_records: list[DisasterMessageRecord] = []
        for record in result.records:
            if db.is_known(record):
                duplicate_records.append(record)
            else:
                new_records.append(record)

        print(f"new records          : {len(new_records)}")
        print(f"duplicate records     : {len(duplicate_records)}")

        will_notify_baseline = was_empty and args.notify_existing
        to_send_preview = new_records if (not was_empty or will_notify_baseline) else []

        print("records that would be sent to Telegram:")
        if not to_send_preview:
            print("  (none)")
        else:
            for record in to_send_preview:
                print(f"  {record_preview(record)}")

        if args.dry_run:
            print("dry-run: no database writes, no Telegram sends performed.")
            return 0

        run_id = db.start_run("poll_once")

        is_baseline_run = was_empty
        for record in new_records:
            db.insert(record, is_baseline=is_baseline_run)

        sent_count = 0
        failed_count = 0

        if args.send:
            should_send_new = (not is_baseline_run) or args.notify_existing
            send_targets: list[DisasterMessageRecord] = list(new_records) if should_send_new else []

            retry_records = db.pending_retry_records() if not is_baseline_run else []

            if send_targets or retry_records:
                with TelegramSender(settings) as sender:
                    for record in [*send_targets, *retry_records]:
                        outcome = _send_via_template_pipeline(sender, db, settings, record)
                        db.update_telegram_result(
                            record.internal_id,
                            status=outcome.status,
                            message_id=outcome.combined_message_id,
                        )
                        if outcome.status == TelegramStatus.TELEGRAM_SENT:
                            sent_count += 1
                        elif outcome.status == TelegramStatus.TELEGRAM_FAILED:
                            failed_count += 1
                            logger.error("Telegram send failed for %s: %s", record.source_id, outcome.error)
            elif not is_baseline_run or args.notify_existing:
                print("nothing to send.")
        elif is_baseline_run and not args.notify_existing:
            print("this run established the baseline; nothing sent to Telegram by default.")

        db.finish_run(
            run_id,
            status="ok",
            fetched_count=result.fetched_count,
            new_count=len(new_records),
            duplicate_count=len(duplicate_records),
            sent_count=sent_count,
            failed_count=failed_count,
        )

    print(f"sent: {sent_count}, failed: {failed_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
