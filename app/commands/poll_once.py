"""Run a single collection + dedup (+ optional Telegram send) cycle.

Usage:
    python -m app.commands.poll_once --dry-run
    python -m app.commands.poll_once --send
    python -m app.commands.poll_once --send --notify-existing

--dry-run performs no database writes and sends nothing; it only reports
what a real run would do.

Without --dry-run, newly-detected records are always stored. Telegram
sending only happens with --send, and only for genuinely new records
(plus any previously collected-but-unsent per-recipient deliveries, to
support retry) unless the database was empty at the start of this run (i.e.
this run is itself acting as the baseline) — in that baseline case nothing
is sent unless --notify-existing is also given, per the "don't notify on
first run" requirement.

Automatic delivery is personal (v0.4.0): each genuinely new SafeCity record
fans out independently to every active personal subscription
(`telegram_subscriptions`), one `telegram_deliveries` row per recipient. One
recipient's failure never blocks another; retries target only that
recipient's failed/pending delivery. There is no single primary recipient
and no `TELEGRAM_CHAT_ID` fallback — see docs/independent_operator_model.md.

This command runs one cycle and exits; app/poller.py calls `run_poll_cycle`
directly in a recurring loop for local continuous operation.
"""

from __future__ import annotations

import argparse
import logging
import sys

from app.collector import CollectorError, EmptyWidgetError, fetch_records
from app.commands._shared import record_preview
from app.config import Settings, load_settings
from app.database import Database, open_database
from app.logging_config import configure_logging
from app.models import DisasterMessageRecord, TelegramStatus
from app.telegram_sender import TelegramSender
from app.template_flow import send_initial_alert

logger = logging.getLogger(__name__)


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


def _send_one_delivery(
    sender: TelegramSender,
    db: Database,
    settings: Settings,
    record: DisasterMessageRecord,
    *,
    chat_id: str,
    delivery_id: int,
    persist_suggestion: bool,
) -> str:
    """Send one personal automatic delivery and record its per-recipient
    outcome. Returns 'sent' | 'failed' | 'pending'. Never raises: isolating
    one recipient's failure so it cannot block delivery to the others."""
    try:
        outcome = send_initial_alert(
            sender,
            db,
            settings,
            record,
            target_chat_id=chat_id,
            persist_suggestion=persist_suggestion,
            enforce_send_enabled=True,
        )
    except Exception as exc:  # noqa: BLE001 - one recipient must never break others
        db.mark_delivery_failed(delivery_id, error=str(exc))
        logger.error("delivery %s failed for %s: %s", delivery_id, record.source_id, exc)
        return "failed"

    if outcome.status == TelegramStatus.TELEGRAM_SENT:
        db.mark_delivery_sent(delivery_id, telegram_message_id=outcome.combined_message_id)
        return "sent"
    if outcome.status == TelegramStatus.TELEGRAM_FAILED:
        db.mark_delivery_failed(delivery_id, error=outcome.error)
        logger.error("delivery %s failed for %s: %s", delivery_id, record.source_id, outcome.error)
        return "failed"
    # PENDING: e.g. the global TELEGRAM_SEND_ENABLED master switch is off.
    # Leave the delivery pending so it fires once the switch is enabled.
    return "pending"


def _fan_out_and_retry(
    db: Database,
    settings: Settings,
    new_records: list[DisasterMessageRecord],
    *,
    is_baseline_run: bool,
) -> tuple[int, int]:
    """Fan out each genuinely new record to every active personal subscription
    and retry any previously failed/pending per-recipient deliveries. Each
    recipient is an independent delivery; one failure never blocks another.
    Returns (sent_count, failed_count) summed across all recipients."""
    subscriptions = db.list_active_subscriptions(settings.telegram_allowed_user_ids)

    # Build the retry set from telegram_deliveries (never messages.telegram_status)
    # BEFORE creating this cycle's new delivery rows, so a brand-new record's
    # own deliveries are not retried in the same pass they were just created.
    # Pass the current allowed-user IDs so a retry authorizes identically to a
    # new delivery — a user removed from TELEGRAM_ALLOWED_USER_IDS is never
    # retried (their historical delivery row is kept for audit only).
    retry_rows = (
        [] if is_baseline_run else db.list_retryable_deliveries(settings.telegram_allowed_user_ids)
    )

    if not subscriptions and new_records:
        for record in new_records:
            logger.warning(
                "no active subscriptions; stored %s without delivery "
                "(no TELEGRAM_CHAT_ID fallback)",
                record.source_id,
            )
            db.update_message_aggregate_status(record.internal_id)

    # Pre-create one pending delivery row per (new record, subscription) so the
    # retry set and the new set never overlap and each recipient is tracked
    # independently. persist_suggestion is emitted once per message only.
    planned: list[tuple[DisasterMessageRecord, str, int, bool]] = []
    for record in new_records:
        first_for_message = True
        for sub in subscriptions:
            delivery_id = db.create_delivery_if_missing(
                message_id=record.internal_id, subscription=sub
            )
            if delivery_id is None:
                continue
            planned.append((record, sub.chat_id, delivery_id, first_for_message))
            first_for_message = False

    print("records that would be sent to Telegram:")
    if not (planned or retry_rows):
        print("  (none)")
        print("nothing to send.")
        return (0, 0)
    for record, _chat_id, _delivery_id, _first in planned:
        print(f"  -> {record_preview(record)}")
    for row in retry_rows:
        print(f"  retry -> delivery {row['delivery_id']} (message {row['message_id']})")

    sent_count = 0
    failed_count = 0
    touched_messages: set[int] = set()
    with TelegramSender(settings) as sender:
        # Retry previous cycles' failed/pending deliveries first.
        for row in retry_rows:
            record = db.get_by_internal_id(row["message_id"])
            if record is None:
                continue
            result = _send_one_delivery(
                sender,
                db,
                settings,
                record,
                chat_id=str(row["sub_chat_id"]),
                delivery_id=row["delivery_id"],
                persist_suggestion=False,
            )
            touched_messages.add(row["message_id"])
            if result == "sent":
                sent_count += 1
            elif result == "failed":
                failed_count += 1

        # Fan out this cycle's new records.
        for record, chat_id, delivery_id, first_for_message in planned:
            result = _send_one_delivery(
                sender,
                db,
                settings,
                record,
                chat_id=str(chat_id),
                delivery_id=delivery_id,
                persist_suggestion=first_for_message,
            )
            touched_messages.add(record.internal_id)
            if result == "sent":
                sent_count += 1
            elif result == "failed":
                failed_count += 1

    for message_id in touched_messages:
        db.update_message_aggregate_status(message_id)

    return (sent_count, failed_count)


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
            # One-time bootstrap so the poller can deliver even if the bot
            # process has not seeded subscriptions yet (no-op once seeded).
            db.seed_subscriptions_if_empty(
                allowed_user_ids=settings.telegram_allowed_user_ids,
                legacy_chat_id=settings.telegram_chat_id or None,
            )
            deliver_new = new_records if (not is_baseline_run or notify_existing) else []
            sent_count, failed_count = _fan_out_and_retry(
                db, settings, deliver_new, is_baseline_run=is_baseline_run
            )
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
