"""Resumable historical backfill collector.

Usage:
    python -m app.commands.backfill_history --target-count 10000 --delay-seconds 1.5
    python -m app.commands.backfill_history --resume
    python -m app.commands.backfill_history --status

Stops only once SQLite confirms the target unique-row count has been
reached (see app/history_backfill.py). SIGINT/SIGTERM trigger a graceful
pause: the in-flight page finishes its current record, progress is saved,
and the process exits cleanly so `--resume` can continue later.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time

from app.config import load_settings
from app.history_backfill import BackfillProgress, HistoryBackfillError, run_backfill
from app.history_database import open_history_database
from app.logging_config import configure_logging

logger = logging.getLogger(__name__)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-count", type=int, default=None, help="unique records to collect")
    parser.add_argument("--delay-seconds", type=float, default=None, help="delay between requests")
    parser.add_argument("--resume", action="store_true", help="resume the last active run")
    parser.add_argument("--status", action="store_true", help="print run status and exit")
    return parser.parse_args(argv)


def _print_progress(progress: BackfillProgress) -> None:
    pct = (progress.unique_count / progress.target_count * 100) if progress.target_count else 0.0
    print(
        f"pages={progress.pages_processed} "
        f"fetched={progress.fetched_count:,} "
        f"inserted={progress.inserted_count:,} "
        f"duplicates={progress.duplicate_count:,} "
        f"malformed={progress.malformed_count:,} "
        f"target={progress.target_count:,} "
        f"progress={pct:.2f}% "
        f"status={progress.status}"
    )


def _print_status(settings) -> int:
    with open_history_database(settings.history_database_path) as db:
        run = db.latest_run()
        unique_count = db.unique_count()
    if run is None:
        print("no backfill run has been started yet.")
        print(f"unique records stored: {unique_count:,}")
        return 0
    print(f"run_id           : {run['run_id']}")
    print(f"status           : {run['status']}")
    print(f"target_count     : {run['target_count']:,}")
    print(f"unique_count     : {unique_count:,}")
    print(f"current_page     : {run['current_page']}")
    print(f"pages_processed  : {run['pages_processed']:,}")
    print(f"fetched_count    : {run['fetched_count']:,}")
    print(f"inserted_count   : {run['inserted_count']:,}")
    print(f"duplicate_count  : {run['duplicate_count']:,}")
    print(f"malformed_count  : {run['malformed_count']:,}")
    print(f"error_count      : {run['error_count']:,}")
    print(f"retry_count      : {run['retry_count']:,}")
    print(f"started_at       : {run['started_at']}")
    print(f"finished_at      : {run['finished_at']}")
    print(f"last_error       : {run['last_error']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = load_settings()
    configure_logging(settings.log_level)

    if args.status:
        return _print_status(settings)

    target_count = (
        args.target_count if args.target_count is not None else settings.history_target_count
    )
    delay_seconds = (
        args.delay_seconds
        if args.delay_seconds is not None
        else settings.history_request_delay_seconds
    )

    stop_requested = {"flag": False}

    def _handle_signal(signum: int, _frame: object) -> None:
        logger.warning("received signal %s; pausing after the current record...", signum)
        stop_requested["flag"] = True

    previous_sigint = signal.signal(signal.SIGINT, _handle_signal)
    previous_sigterm = signal.signal(signal.SIGTERM, _handle_signal)

    try:
        with open_history_database(settings.history_database_path) as db:
            try:
                result = run_backfill(
                    db,
                    target_count=target_count,
                    delay_seconds=delay_seconds,
                    timeout_seconds=settings.history_request_timeout_seconds,
                    max_retries=settings.history_max_retries,
                    resume=args.resume,
                    sleep_fn=time.sleep,
                    should_stop=lambda: stop_requested["flag"],
                    progress_cb=_print_progress,
                )
            except HistoryBackfillError as exc:
                print(f"FAILED: {exc}", file=sys.stderr)
                return 1
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
        signal.signal(signal.SIGTERM, previous_sigterm)

    print(f"final status: {result.status}")
    return 0 if result.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
