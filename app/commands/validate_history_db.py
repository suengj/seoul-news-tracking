"""Validate the historical raw-message database.

Usage: python -m app.commands.validate_history_db

Exits non-zero if any structural problem is found (duplicate source_id or
raw_hash groups, missing bodies). A count below the run's target is reported
but is not itself a failure — use this after a backfill run to confirm the
"exactly N unique records" requirement before declaring completion.
"""

from __future__ import annotations

from app.config import load_settings
from app.history_database import open_history_database
from app.logging_config import configure_logging


def main() -> int:
    settings = load_settings()
    configure_logging(settings.log_level)

    db_path = settings.history_database_path
    if not db_path.exists():
        print(f"FAILED: database does not exist at {db_path}")
        return 1

    with open_history_database(db_path) as db:
        stats = db.validation_stats()
        run = db.latest_run()

    size_bytes = db_path.stat().st_size

    print(f"database path            : {db_path}")
    print(f"database size            : {size_bytes:,} bytes ({size_bytes / 1_048_576:.2f} MiB)")
    print(f"unique record count      : {stats['total']:,}")
    print(f"duplicate source_id groups: {stats['duplicate_source_id_groups']}")
    print(f"duplicate raw_hash groups : {stats['duplicate_raw_hash_groups']}")
    print(f"missing body count       : {stats['missing_body']}")
    print(f"missing sent_at count    : {stats['missing_sent_at']}")
    print(f"missing sender count     : {stats['missing_sender']}")
    print(f"oldest sent_at           : {stats['oldest_sent_at']}")
    print(f"newest sent_at           : {stats['newest_sent_at']}")
    print(f"source_page range        : {stats['min_source_page']} .. {stats['max_source_page']}")

    if run is not None:
        print(f"latest run status        : {run['status']}")
        print(f"latest run target_count  : {run['target_count']:,}")
        print(f"latest run pages_processed: {run['pages_processed']:,}")
        print(f"latest run fetched_count : {run['fetched_count']:,}")
        print(f"latest run duplicate_count: {run['duplicate_count']:,}")
        print(f"latest run malformed_count: {run['malformed_count']:,}")
        print(f"latest run error_count   : {run['error_count']:,}")
        print(f"latest run retry_count   : {run['retry_count']:,}")
        print(f"latest run started_at    : {run['started_at']}")
        print(f"latest run finished_at   : {run['finished_at']}")
        approx_requests = (
            run["pages_processed"]
            + run["inserted_count"]
            + run["malformed_count"]
            + run["retry_count"]
        )
        print(f"approx. total requests   : {approx_requests:,}")

    ok = (
        stats["duplicate_source_id_groups"] == 0
        and stats["duplicate_raw_hash_groups"] == 0
        and stats["missing_body"] == 0
    )
    if not ok:
        print("FAILED: structural validation problems found above.")
        return 1

    print("OK: no duplicate source_id/raw_hash groups, no missing bodies.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
