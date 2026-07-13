"""Local recurring polling loop.

A single-threaded, sequential loop: each cycle fully completes (collect,
dedup, send, update health state) before the next one is even considered,
which is what makes "no overlapping poll cycles" trivially true here — there
is never more than one poll in flight. If a cycle runs longer than
`POLL_INTERVAL_SECONDS`, the overrun is logged and the next cycle starts
immediately instead of waiting out a now-meaningless sleep.

This is intentionally not a scheduler (no cron, no systemd, no VPS). See
app/future/scheduler.py for that later work.
"""

from __future__ import annotations

import logging
import signal
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from app.commands.poll_once import run_poll_cycle
from app.config import Settings
from app.database import open_database

logger = logging.getLogger(__name__)

SEOUL_TZ = ZoneInfo("Asia/Seoul")
POLLER_MAX_BACKOFF_SECONDS = 300.0
SLEEP_GRANULARITY_SECONDS = 1.0


class PollCycleError(RuntimeError):
    """Raised internally when a poll cycle reports failure, to drive backoff."""


class Poller:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._running = True
        self._consecutive_errors = 0

    def stop(self, *_signal_args: object) -> None:
        logger.info("poller received stop signal")
        self._running = False

    def install_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)

    def run_forever(self, *, max_cycles: int | None = None) -> None:
        """Run cycles until stopped. `max_cycles` is for tests only."""
        logger.info(
            "poller starting: interval=%ss, cleanup_interval=%sh",
            self.settings.poll_interval_seconds,
            self.settings.cleanup_interval_hours,
        )
        cycles_run = 0
        while self._running:
            wait_seconds = self._run_one_cycle()
            cycles_run += 1
            if max_cycles is not None and cycles_run >= max_cycles:
                break
            self._sleep_interruptible(wait_seconds)
        logger.info("poller stopped cleanly after %d cycle(s)", cycles_run)

    def _run_one_cycle(self) -> float:
        cycle_start = time.monotonic()
        error: Exception | None = None
        try:
            self._run_cycle_if_enabled()
        except Exception as exc:  # noqa: BLE001 - poller must never die on a bad cycle
            error = exc
            self._consecutive_errors += 1
            logger.exception("poll cycle failed (consecutive_errors=%d)", self._consecutive_errors)
        else:
            self._consecutive_errors = 0

        self._maybe_run_cleanup()

        elapsed = time.monotonic() - cycle_start
        if error is not None:
            backoff = min(
                self.settings.poll_interval_seconds * (2 ** min(self._consecutive_errors - 1, 5)),
                POLLER_MAX_BACKOFF_SECONDS,
            )
            logger.warning("waiting %.1fs before retrying after error", backoff)
            return backoff
        if elapsed > self.settings.poll_interval_seconds:
            logger.warning(
                "poll cycle took %.1fs, longer than interval %ss; starting next cycle immediately",
                elapsed,
                self.settings.poll_interval_seconds,
            )
            return 0.0
        return self.settings.poll_interval_seconds - elapsed

    def _run_cycle_if_enabled(self) -> None:
        with open_database(self.settings.database_path) as db:
            enabled = db.is_polling_enabled()

        if not enabled:
            logger.info("polling is paused; skipping Seoul SafeCity request this cycle")
            return

        return_code = run_poll_cycle(self.settings, send=True, notify_existing=False)
        if return_code != 0:
            raise PollCycleError(f"poll cycle exited with code {return_code}")

    def _maybe_run_cleanup(self) -> None:
        now = datetime.now(tz=SEOUL_TZ)
        with open_database(self.settings.database_path) as db:
            if not db.should_run_cleanup(
                cleanup_interval_hours=self.settings.cleanup_interval_hours, now=now
            ):
                return
            logger.info("running scheduled retention cleanup")
            result = db.cleanup_execute(
                message_retention_days=self.settings.message_retention_days,
                run_history_retention_days=self.settings.run_history_retention_days,
                tombstone_retention_days=self.settings.tombstone_retention_days,
                now=now,
            )
            logger.info(
                "cleanup done: messages=%d run_history=%d tombstones=%d",
                result.message_rows_eligible,
                result.run_history_rows_eligible,
                result.tombstone_rows_eligible,
            )

    def _sleep_interruptible(self, seconds: float) -> None:
        """Sleep in small increments so a signal-driven stop() is noticed promptly."""
        deadline = time.monotonic() + max(seconds, 0.0)
        while self._running and time.monotonic() < deadline:
            time.sleep(min(SLEEP_GRANULARITY_SECONDS, max(deadline - time.monotonic(), 0.0)))
