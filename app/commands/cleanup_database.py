"""Manual retention cleanup.

python -m app.commands.cleanup_database --dry-run
python -m app.commands.cleanup_database --confirm

--dry-run (default if neither flag given) reports what would be deleted and
makes no changes. --confirm actually deletes. Message deletions are
tombstoned first (source_id + raw_hash only) so dedup keeps working across
cleanup; see docs/database_retention.md.

Never touches template_suggestions/template_actions/template_previews/
template_decisions/ai_generations — a confirmed decision must outlive the
source message's retention window (see docs/database_retention.md).
"""

from __future__ import annotations

import argparse

from app.config import load_settings
from app.database import CleanupCounts, open_database
from app.logging_config import configure_logging


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", help="report only, no changes (default)")
    group.add_argument("--confirm", action="store_true", help="actually delete expired rows")
    return parser.parse_args(argv)


def _print_counts(counts: CleanupCounts) -> None:
    print(f"message rows eligible for deletion       : {counts.message_rows_eligible}")
    print(f"run-history rows eligible for deletion    : {counts.run_history_rows_eligible}")
    print(f"tombstone rows eligible for deletion       : {counts.tombstone_rows_eligible}")
    print(f"estimated post-cleanup message count       : {counts.post_cleanup_message_count}")
    print(f"estimated post-cleanup run-history count   : {counts.post_cleanup_run_history_count}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = load_settings()
    configure_logging(settings.log_level)

    print(f"MESSAGE_RETENTION_DAYS      = {settings.message_retention_days}")
    print(f"RUN_HISTORY_RETENTION_DAYS  = {settings.run_history_retention_days}")
    print(f"TOMBSTONE_RETENTION_DAYS    = {settings.tombstone_retention_days}")
    print()

    with open_database(settings.database_path) as db:
        if args.confirm:
            counts = db.cleanup_execute(
                message_retention_days=settings.message_retention_days,
                run_history_retention_days=settings.run_history_retention_days,
                tombstone_retention_days=settings.tombstone_retention_days,
            )
            print("cleanup executed (rows deleted):")
            _print_counts(counts)
        else:
            counts = db.cleanup_preview(
                message_retention_days=settings.message_retention_days,
                run_history_retention_days=settings.run_history_retention_days,
                tombstone_retention_days=settings.tombstone_retention_days,
            )
            print("dry-run (no changes made):")
            _print_counts(counts)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
