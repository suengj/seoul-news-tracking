"""Establish the non-notifying baseline on first run.

Fetches the currently retrievable records and stores them as the initial
baseline WITHOUT sending anything to Telegram, so pre-existing messages are
never mistaken for new alerts. Idempotent: if the database already has
records, this command reports that and makes no changes.

Usage: python -m app.commands.establish_baseline
"""

from __future__ import annotations

import sys

from app.collector import CollectorError, fetch_records
from app.config import load_settings
from app.database import open_database
from app.logging_config import configure_logging


def main() -> int:
    settings = load_settings()
    configure_logging(settings.log_level)

    with open_database(settings.database_path) as db:
        if not db.is_empty:
            print(
                "database already has records; baseline already established. "
                "No changes made."
            )
            return 0

        run_id = db.start_run("establish_baseline")

        try:
            result = fetch_records()
        except CollectorError as exc:
            db.finish_run(run_id, status="failed", detail=str(exc))
            print(f"FAILED: {exc}", file=sys.stderr)
            return 1

        for record in result.records:
            db.insert(record, is_baseline=True)

        db.finish_run(
            run_id,
            status="ok",
            fetched_count=result.fetched_count,
            new_count=len(result.records),
            detail="baseline established; nothing sent to Telegram",
        )

    print(f"baseline established: {result.fetched_count} record(s) stored, none sent to Telegram.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
