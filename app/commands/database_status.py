"""Report local database size and health.

Usage: python -m app.commands.database_status
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

from app.config import load_settings
from app.database import open_database
from app.logging_config import configure_logging

SEOUL_TZ = ZoneInfo("Asia/Seoul")


def main() -> int:
    settings = load_settings()
    configure_logging(settings.log_level)

    db_size_bytes = settings.database_path.stat().st_size if settings.database_path.exists() else 0

    with open_database(settings.database_path) as db:
        report = db.status_report()
        state = db.get_system_state()
        cleanup_counts = db.cleanup_preview(
            message_retention_days=settings.message_retention_days,
            run_history_retention_days=settings.run_history_retention_days,
            tombstone_retention_days=settings.tombstone_retention_days,
        )

    print(f"database file size          : {db_size_bytes / 1024:.1f} KB")
    print(f"total message rows          : {report['total_messages']}")
    print(f"baseline rows                : {report['baseline_messages']}")
    print("messages by telegram status  :")
    for status, count in sorted(report["messages_by_telegram_status"].items()):
        print(f"  {status:<20} {count}")
    print(f"oldest source sent_at        : {report['oldest_sent_at']}")
    print(f"newest source sent_at        : {report['newest_sent_at']}")
    print(f"run-history row count        : {report['run_history_count']}")
    print(f"oldest run-history entry     : {report['oldest_run_history_at']}")
    print(f"newest run-history entry     : {report['newest_run_history_at']}")
    print()
    print(f"MESSAGE_RETENTION_DAYS      = {settings.message_retention_days}")
    print(f"RUN_HISTORY_RETENTION_DAYS  = {settings.run_history_retention_days}")
    print(f"CLEANUP_INTERVAL_HOURS      = {settings.cleanup_interval_hours}")
    print()
    print(f"polling enabled              : {state.polling_enabled}")
    print(f"last successful poll         : {state.last_successful_poll_at}")
    print(f"last poll error              : {state.last_poll_error or '(none)'}")
    print(f"last cleanup run             : {state.last_cleanup_at}")
    print()
    print("cleanup eligibility (at current retention settings):")
    print(f"  message rows eligible      : {cleanup_counts.message_rows_eligible}")
    print(f"  run-history rows eligible  : {cleanup_counts.run_history_rows_eligible}")
    print(f"  tombstone rows eligible    : {cleanup_counts.tombstone_rows_eligible}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
