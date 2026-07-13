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

This command runs one cycle and exits; app/poller.py calls `run_poll_cycle`
directly in a recurring loop for local continuous operation.
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
from app.telegram_sender import TelegramSender, TelegramSendOutcome
from app.template_flow import build_initial_alert

logger = logging.getLogger(__name__)


def _send_initial_alert(
    sender: TelegramSender, db: Database, settings: Settings, record: DisasterMessageRecord
) -> TelegramSendOutcome:
    """Build and send the Service v1 initial alert, then persist the rule
    engine's (informational-only) suggestion.

    The Telegram send happens first and its outcome is always returned
    as-is: a failure to persist `template_suggestions` afterward must never
    look like a failed send, or the record's `telegram_status` would be
    wrongly reverted from a real success and risk a duplicate delivery on
    the next retry pass.
    """
    text, keyboard, recommended, candidates = build_initial_alert(record, settings)
    outcome = sender.send_plain_text(text, reply_markup=keyboard)

    try:
        db.insert_template_suggestion(
            message_id=record.internal_id,
            recommended_template_id=recommended.template_id if recommended else None,
            rule_score=recommended.rule_score if recommended else None,
            candidates_json=json.dumps([vars(c) for c in candidates], ensure_ascii=False),
            extraction_json="{}",
            rendered_text=None,
        )
    except Exception:
        logger.exception(
            "failed to store template_suggestion for source_id=%s (send already completed)",
            record.source_id,
        )

    return outcome


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="report only, no DB writes, no sends"
    )
    parser.add_argument(
        "--send", action="store_true", help="attempt Telegram delivery for new records"
    )
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

    if args.dry_run:
        # Fully read-only: not even a run_history row is written.
        try:
            result = fetch_records()
        except (EmptyWidgetError, CollectorError) as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            return 1

        print(f"selected method     : {result.method}")
        print(f"records fetched      : {result.fetched_count}")
        print(f"full text confirmed  : {result.full_text_confirmed}")

        with open_database(settings.database_path) as db:
            was_empty = db.is_empty
            new_records = [r for r in result.records if not db.is_known(r)]
            duplicate_records = [r for r in result.records if db.is_known(r)]

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

        print("dry-run: no database writes, no Telegram sends performed.")
        return 0

    return run_poll_cycle(settings, send=args.send, notify_existing=args.notify_existing)


def run_poll_cycle(settings: Settings, *, send: bool, notify_existing: bool) -> int:
    """Execute one real (non-dry-run) poll cycle. Used by both the CLI and app/poller.py."""
    with open_database(settings.database_path) as db:
        run_id = db.start_run("poll_once")

        try:
            result = fetch_records()
        except (EmptyWidgetError, CollectorError) as exc:
            db.record_poll_error(str(exc))
            db.finish_run(run_id, status="failed", detail=str(exc), store_successful_noop_runs=True)
            print(f"FAILED: {exc}", file=sys.stderr)
            return 1

        print(f"selected method     : {result.method}")
        print(f"records fetched      : {result.fetched_count}")
        print(f"full text confirmed  : {result.full_text_confirmed}")

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

        is_baseline_run = was_empty
        for record in new_records:
            db.insert(record, is_baseline=is_baseline_run)

        sent_count = 0
        failed_count = 0

        if send:
            should_send_new = (not is_baseline_run) or notify_existing
            send_targets: list[DisasterMessageRecord] = list(new_records) if should_send_new else []
            retry_records = db.pending_retry_records() if not is_baseline_run else []

            print("records that would be sent to Telegram:")
            if not (send_targets or retry_records):
                print("  (none)")
            else:
                for record in [*send_targets, *retry_records]:
                    print(f"  {record_preview(record)}")

            if send_targets or retry_records:
                with TelegramSender(settings) as sender:
                    for record in [*send_targets, *retry_records]:
                        outcome = _send_initial_alert(sender, db, settings, record)
                        db.update_telegram_result(
                            record.internal_id,
                            status=outcome.status,
                            message_id=outcome.combined_message_id,
                        )
                        if outcome.status == TelegramStatus.TELEGRAM_SENT:
                            sent_count += 1
                        elif outcome.status == TelegramStatus.TELEGRAM_FAILED:
                            failed_count += 1
                            logger.error(
                                "Telegram send failed for %s: %s", record.source_id, outcome.error
                            )
            else:
                print("nothing to send.")
        elif is_baseline_run and not notify_existing:
            print("this run established the baseline; nothing sent to Telegram by default.")

        db.record_poll_success(found_new=len(new_records) > 0)
        db.finish_run(
            run_id,
            status="ok",
            fetched_count=result.fetched_count,
            new_count=len(new_records),
            duplicate_count=len(duplicate_records),
            sent_count=sent_count,
            failed_count=failed_count,
            store_successful_noop_runs=settings.store_successful_noop_runs,
        )

    print(f"sent: {sent_count}, failed: {failed_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
