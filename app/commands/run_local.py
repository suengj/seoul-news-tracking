"""Convenience command: run the poller and the Telegram bot together locally.

Launches `run_poller` and `run_telegram_bot` as two child processes (the
preferred separation — see docs/local_runtime.md for why), forwards Ctrl+C
(SIGINT) and SIGTERM to a clean shutdown of both, and refuses to start if
another `run_local` (or a lock file left by a stale one) is already active.

Usage: python -m app.commands.run_local
"""

from __future__ import annotations

import logging
import signal
import subprocess
import sys
import time

from app.config import load_settings
from app.logging_config import configure_logging
from app.process_lock import SingleInstanceLock

logger = logging.getLogger(__name__)

CHILD_STOP_TIMEOUT_SECONDS = 10
POLL_LOOP_INTERVAL_SECONDS = 0.5


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

    procs: dict[str, subprocess.Popen] = {}
    stop_requested = False
    exit_code = 0

    def _request_stop(*_signal_args: object) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)

    try:
        for name, module in (
            ("poller", "app.commands.run_poller"),
            ("telegram_bot", "app.commands.run_telegram_bot"),
        ):
            proc = subprocess.Popen([sys.executable, "-m", module])
            procs[name] = proc
            logger.info("started %s (pid=%d)", name, proc.pid)

        while not stop_requested:
            for name, proc in procs.items():
                rc = proc.poll()
                if rc is not None:
                    logger.error("%s exited unexpectedly with code %s", name, rc)
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
