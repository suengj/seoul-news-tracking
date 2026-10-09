"""Convenience command: run the poller and the Telegram bot together locally.

Launches `run_poller` and `run_telegram_bot` as two child processes (the
preferred separation — see docs/local_runtime.md for why), forwards Ctrl+C
(SIGINT) and SIGTERM to a clean shutdown of both, and refuses to start if
another `run_local` (or a lock file left by a stale one) is already active.

If a child exits unexpectedly, only that child is restarted (with backoff) —
the sibling is left running. A child that crashes repeatedly in a short
window (a persistent problem, not a transient blip) is not retried forever:
after MAX_RESTARTS_IN_WINDOW crashes within RESTART_WINDOW_SECONDS, the whole
service gives up and exits nonzero, same as before this existed. Without
this, any single unhandled exception in either child took the whole service
down until a human noticed and restarted it manually (observed live:
~12.5 hours of downtime from one httpx.ReadError — see
app/telegram_bot.py's TransportError handling, which is now itself also
fixed, but this is the general-purpose backstop for the next unforeseen one).

Usage: python -m app.commands.run_local
"""

from __future__ import annotations

import logging
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from app.config import load_settings
from app.cutover_fence import FENCE_REFUSAL_EXIT_CODE
from app.logging_config import configure_logging
from app.process_lock import SingleInstanceLock

logger = logging.getLogger(__name__)

CHILD_STOP_TIMEOUT_SECONDS = 10
POLL_LOOP_INTERVAL_SECONDS = 0.5

CHILD_SPECS: tuple[tuple[str, str], ...] = (
    ("poller", "app.commands.run_poller"),
    ("telegram_bot", "app.commands.run_telegram_bot"),
)

RESTART_INITIAL_BACKOFF_SECONDS = 2.0
RESTART_MAX_BACKOFF_SECONDS = 60.0
RESTART_WINDOW_SECONDS = 600.0
MAX_RESTARTS_IN_WINDOW = 5
FENCE_REFUSAL_RETRY_SECONDS = 300.0


@dataclass
class _RestartTracker:
    """Per-child crash-loop bookkeeping, kept separate from subprocess I/O
    so the restart/give-up decision is unit-testable without spawning real
    processes. One instance per child (poller, telegram_bot).

    Counts crashes in a rolling RESTART_WINDOW_SECONDS window regardless of
    how long each individual run lasted. An earlier version reset the
    counter on any run that survived 60s, which meant a child crashing every
    ~65s would restart forever and never trip the give-up guard — exactly
    the failure mode this exists to catch, just at a slower cadence. Any
    crash frequency that exceeds MAX_RESTARTS_IN_WINDOW within the window is
    treated as a real problem, deserving human attention rather than
    infinite silent retries."""

    backoff: float = RESTART_INITIAL_BACKOFF_SECONDS
    restart_count: int = 0
    window_start: float = field(default_factory=time.monotonic)

    def next_backoff_or_give_up(self) -> float | None:
        """Call once per crash. Returns the backoff (seconds) to wait
        before restarting, or None if the crash-loop guard says give up —
        this child has crashed too many times within the rolling window to
        keep retrying blindly."""
        now = time.monotonic()
        if now - self.window_start > RESTART_WINDOW_SECONDS:
            self.restart_count = 0
            self.window_start = now
            self.backoff = RESTART_INITIAL_BACKOFF_SECONDS

        self.restart_count += 1
        if self.restart_count > MAX_RESTARTS_IN_WINDOW:
            return None

        backoff = self.backoff
        self.backoff = min(self.backoff * 2, RESTART_MAX_BACKOFF_SECONDS)
        return backoff


def _spawn(module: str) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-m", module])


_SLEEP_STEP_SECONDS = 0.5


def _interruptible_sleep(duration: float, stop_requested: Callable[[], bool] | None) -> None:
    """Sleep in short increments instead of one long time.sleep(duration),
    so a shutdown signal is honored within _SLEEP_STEP_SECONDS instead of
    only after the full backoff elapses. Matters because backoff can reach
    RESTART_MAX_BACKOFF_SECONDS (60s) — launchd's default ExitTimeOut is 20s,
    so an uninterruptible sleep here risked launchd SIGKILLing run_local
    mid-backoff, bypassing its finally-block cleanup and orphaning children."""
    if stop_requested is None:
        time.sleep(duration)
        return

    remaining = duration
    while remaining > 0 and not stop_requested():
        chunk = min(_SLEEP_STEP_SECONDS, remaining)
        time.sleep(chunk)
        remaining -= chunk


def _check_children(
    procs: dict[str, subprocess.Popen],
    started_at: dict[str, float],
    trackers: dict[str, _RestartTracker],
    *,
    child_specs: tuple[tuple[str, str], ...] = CHILD_SPECS,
    stop_requested: Callable[[], bool] | None = None,
    retry_after: dict[str, float] | None = None,
) -> bool:
    """Poll every child once. Any that exited unexpectedly are restarted in
    place (with backoff), leaving the others untouched. Returns False if a
    child's crash-loop guard gives up (caller should stop the whole
    service), True otherwise.

    `stop_requested` is an optional zero-arg callable checked right after
    each backoff sleep, so a shutdown signal received mid-backoff skips the
    now-pointless respawn instead of starting a process only to immediately
    terminate it. Tests can omit it.
    """
    for name, module in child_specs:
        if retry_after is not None and name in retry_after:
            if time.monotonic() < retry_after[name]:
                continue
            del retry_after[name]
            new_proc = _spawn(module)
            procs[name] = new_proc
            started_at[name] = time.monotonic()
            logger.info("restarted %s after cutover fence refusal (pid=%d)", name, new_proc.pid)
            continue
        proc = procs[name]
        rc = proc.poll()
        if rc is None:
            continue

        uptime = time.monotonic() - started_at[name]
        logger.error(
            "%s exited unexpectedly with code %s after %.1fs uptime", name, rc, uptime
        )
        if name == "telegram_bot" and rc == FENCE_REFUSAL_EXIT_CODE:
            logger.error(
                "telegram_bot refused the cutover fence (exit code %s); "
                "poller remains active and bot retry is scheduled in %.0fs",
                rc,
                FENCE_REFUSAL_RETRY_SECONDS,
            )
            if retry_after is not None:
                retry_after[name] = time.monotonic() + FENCE_REFUSAL_RETRY_SECONDS
            continue
        tracker = trackers[name]
        backoff = tracker.next_backoff_or_give_up()
        if backoff is None:
            logger.error(
                "%s crashed too many times within %.0fs; giving up "
                "and stopping the whole service",
                name,
                RESTART_WINDOW_SECONDS,
            )
            return False

        logger.warning("restarting %s in %.1fs after unexpected exit", name, backoff)
        _interruptible_sleep(backoff, stop_requested)
        if stop_requested is not None and stop_requested():
            break

        new_proc = _spawn(module)
        procs[name] = new_proc
        started_at[name] = time.monotonic()
        logger.info("restarted %s (pid=%d)", name, new_proc.pid)
    return True


def main() -> int:
    settings = load_settings()
    configure_logging(settings.log_level)

    lock_path = settings.database_path.parent / "run_local.lock"
    try:
        lock = SingleInstanceLock(lock_path)
        lock.acquire()
    except RuntimeError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1

    stop_requested = False
    exit_code = 0

    def _request_stop(*_signal_args: object) -> None:
        nonlocal stop_requested
        stop_requested = True

    def _stop_requested_now() -> bool:
        return stop_requested

    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)

    procs: dict[str, subprocess.Popen] = {}
    started_at: dict[str, float] = {}
    trackers: dict[str, _RestartTracker] = {}
    retry_after: dict[str, float] = {}

    try:
        for name, module in CHILD_SPECS:
            proc = _spawn(module)
            procs[name] = proc
            started_at[name] = time.monotonic()
            trackers[name] = _RestartTracker()
            logger.info("started %s (pid=%d)", name, proc.pid)

        while not stop_requested:
            if not _check_children(
                procs,
                started_at,
                trackers,
                stop_requested=_stop_requested_now,
                retry_after=retry_after,
            ):
                exit_code = 1
                stop_requested = True
                break

            if not stop_requested:
                time.sleep(POLL_LOOP_INTERVAL_SECONDS)
    finally:
        _terminate_all(procs)
        lock.release()

    logger.info("run_local stopped cleanly (exit_code=%d)", exit_code)
    return exit_code


def _terminate_all(procs: dict[str, subprocess.Popen]) -> None:
    for name, proc in procs.items():
        if proc.poll() is None:
            logger.info("terminating %s (pid=%d)", name, proc.pid)
            proc.terminate()

    deadline = time.monotonic() + CHILD_STOP_TIMEOUT_SECONDS
    for name, proc in procs.items():
        remaining = max(deadline - time.monotonic(), 0.0)
        try:
            proc.wait(timeout=remaining)
            logger.info("%s stopped (exit code %s)", name, proc.returncode)
        except subprocess.TimeoutExpired:
            logger.warning("%s did not stop in time; killing", name)
            proc.kill()
            proc.wait()
            logger.info("%s killed", name)


if __name__ == "__main__":
    raise SystemExit(main())
