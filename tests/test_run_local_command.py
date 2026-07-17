from __future__ import annotations

import subprocess
import sys
import time

import pytest

from app.commands import run_local
from app.process_lock import SingleInstanceLock


@pytest.fixture
def env_setup(tmp_path, monkeypatch):
    db_path = tmp_path / "run_local_test.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setenv("LOG_LEVEL", "ERROR")
    return db_path


def test_refuses_to_start_second_instance(env_setup):
    lock_path = env_setup.parent / "run_local.lock"
    held = SingleInstanceLock(lock_path)
    held.acquire()
    try:
        rc = run_local.main()
        assert rc == 1
    finally:
        held.release()


def test_terminate_all_stops_real_child_processes_cleanly():
    """Exercises _terminate_all against real (trivial) long-running
    subprocesses to confirm graceful termination and no leftover processes —
    the same mechanism run_local uses to stop the poller/bot children."""
    procs = {
        "child_a": subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"]),
        "child_b": subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"]),
    }
    assert all(p.poll() is None for p in procs.values())

    run_local._terminate_all(procs)

    time.sleep(0.2)
    for name, proc in procs.items():
        assert proc.poll() is not None, f"{name} should have been terminated"


def test_terminate_all_kills_processes_that_ignore_sigterm(monkeypatch):
    """A child that doesn't exit after SIGTERM must be force-killed rather
    than leaving run_local hanging or leaking a duplicate worker."""
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)",
        ]
    )
    time.sleep(0.2)  # let it install the SIG_IGN handler

    monkeypatch.setattr(run_local, "CHILD_STOP_TIMEOUT_SECONDS", 1)
    run_local._terminate_all({"stubborn": proc})

    assert proc.poll() is not None


# -- _RestartTracker: pure crash-loop decision logic, no subprocess I/O ------


def test_first_crash_returns_initial_backoff():
    tracker = run_local._RestartTracker()
    tracker.record_healthy_run(uptime_seconds=0.0)
    assert tracker.next_backoff_or_give_up() == run_local.RESTART_INITIAL_BACKOFF_SECONDS


def test_consecutive_crashes_double_backoff_up_to_cap():
    tracker = run_local._RestartTracker()
    backoffs = []
    for _ in range(8):
        tracker.record_healthy_run(uptime_seconds=0.0)
        backoff = tracker.next_backoff_or_give_up()
        if backoff is None:
            break
        backoffs.append(backoff)

    assert backoffs[0] == run_local.RESTART_INITIAL_BACKOFF_SECONDS
    assert backoffs == sorted(backoffs)  # strictly non-decreasing
    assert max(backoffs) <= run_local.RESTART_MAX_BACKOFF_SECONDS


def test_gives_up_after_max_restarts_in_window():
    tracker = run_local._RestartTracker()
    results = []
    for _ in range(run_local.MAX_RESTARTS_IN_WINDOW + 2):
        tracker.record_healthy_run(uptime_seconds=0.0)
        results.append(tracker.next_backoff_or_give_up())

    assert results[: run_local.MAX_RESTARTS_IN_WINDOW].count(None) == 0
    assert results[-1] is None


def test_healthy_run_resets_counter_and_backoff():
    tracker = run_local._RestartTracker()
    for _ in range(3):
        tracker.record_healthy_run(uptime_seconds=0.0)
        tracker.next_backoff_or_give_up()
    assert tracker.restart_count == 3
    assert tracker.backoff > run_local.RESTART_INITIAL_BACKOFF_SECONDS

    # This crash followed a long, healthy run — treated as a fresh problem.
    tracker.record_healthy_run(uptime_seconds=run_local.MIN_HEALTHY_UPTIME_SECONDS + 1)
    assert tracker.restart_count == 0
    assert tracker.next_backoff_or_give_up() == run_local.RESTART_INITIAL_BACKOFF_SECONDS


def test_short_uptime_does_not_reset_crash_loop_counter():
    tracker = run_local._RestartTracker()
    tracker.record_healthy_run(uptime_seconds=0.0)
    tracker.next_backoff_or_give_up()

    tracker.record_healthy_run(uptime_seconds=run_local.MIN_HEALTHY_UPTIME_SECONDS - 1)
    assert tracker.restart_count == 1  # not reset


def test_window_expiry_resets_counter():
    tracker = run_local._RestartTracker()
    for _ in range(run_local.MAX_RESTARTS_IN_WINDOW):
        tracker.record_healthy_run(uptime_seconds=0.0)
        tracker.next_backoff_or_give_up()
    assert tracker.restart_count == run_local.MAX_RESTARTS_IN_WINDOW

    # Simulate the rolling window having elapsed since the first crash in it.
    tracker.window_start -= run_local.RESTART_WINDOW_SECONDS + 1

    tracker.record_healthy_run(uptime_seconds=0.0)
    backoff = tracker.next_backoff_or_give_up()
    assert backoff == run_local.RESTART_INITIAL_BACKOFF_SECONDS
    assert tracker.restart_count == 1


# -- _check_children: restart-in-place integration, real trivial subprocesses -


def _trivial_exit(code: int) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", f"raise SystemExit({code})"])


def _trivial_sleep() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])


def test_check_children_restarts_only_the_crashed_child(monkeypatch):
    """The core behavior this feature exists for: a crashed sibling must not
    take down a healthy one."""
    monkeypatch.setattr(run_local, "RESTART_INITIAL_BACKOFF_SECONDS", 0.01)

    healthy = _trivial_sleep()
    crashed = _trivial_exit(1)
    time.sleep(0.3)  # let `crashed` actually exit before polling it
    try:
        procs = {"healthy": healthy, "crashed": crashed}
        started_at = {"healthy": time.monotonic(), "crashed": time.monotonic()}
        trackers = {"healthy": run_local._RestartTracker(), "crashed": run_local._RestartTracker()}

        def fake_spawn(module: str) -> subprocess.Popen:
            return _trivial_sleep()

        monkeypatch.setattr(run_local, "_spawn", fake_spawn)

        ok = run_local._check_children(
            procs,
            started_at,
            trackers,
            child_specs=(("healthy", "unused"), ("crashed", "unused")),
        )

        assert ok is True
        assert procs["healthy"] is healthy  # untouched
        assert healthy.poll() is None  # still running
        assert procs["crashed"] is not crashed  # replaced by a new process
        assert procs["crashed"].poll() is None  # the replacement is alive
    finally:
        for proc in (healthy, crashed, procs["crashed"]):
            if proc.poll() is None:
                proc.kill()
                proc.wait()


def test_check_children_gives_up_after_repeated_crashes(monkeypatch):
    monkeypatch.setattr(run_local, "RESTART_INITIAL_BACKOFF_SECONDS", 0.01)
    monkeypatch.setattr(run_local, "MAX_RESTARTS_IN_WINDOW", 1)

    def fake_spawn(module: str) -> subprocess.Popen:
        return _trivial_exit(1)

    monkeypatch.setattr(run_local, "_spawn", fake_spawn)

    crashed = _trivial_exit(1)
    time.sleep(0.3)
    procs = {"flaky": crashed}
    started_at = {"flaky": time.monotonic()}
    trackers = {"flaky": run_local._RestartTracker()}

    # First check: restarts once (attempt 1, within MAX_RESTARTS_IN_WINDOW=1).
    ok = run_local._check_children(
        procs, started_at, trackers, child_specs=(("flaky", "unused"),)
    )
    assert ok is True
    time.sleep(0.3)  # let the replacement (also exit(1)) actually exit

    # Second check: this is attempt 2, over the limit — must give up.
    ok = run_local._check_children(
        procs, started_at, trackers, child_specs=(("flaky", "unused"),)
    )
    assert ok is False
