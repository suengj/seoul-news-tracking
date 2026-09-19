from __future__ import annotations

import subprocess
import sys
import time

import pytest

from app.commands import run_local
from app.cutover_fence import FENCE_REFUSAL_EXIT_CODE
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
    assert tracker.next_backoff_or_give_up() == run_local.RESTART_INITIAL_BACKOFF_SECONDS


def test_consecutive_crashes_double_backoff_up_to_cap():
    tracker = run_local._RestartTracker()
    backoffs = []
    for _ in range(8):
        backoff = tracker.next_backoff_or_give_up()
        if backoff is None:
            break
        backoffs.append(backoff)

    assert backoffs[0] == run_local.RESTART_INITIAL_BACKOFF_SECONDS
    assert backoffs == sorted(backoffs)  # strictly non-decreasing
    assert max(backoffs) <= run_local.RESTART_MAX_BACKOFF_SECONDS


def test_gives_up_after_max_restarts_in_window():
    tracker = run_local._RestartTracker()
    results = [tracker.next_backoff_or_give_up() for _ in range(run_local.MAX_RESTARTS_IN_WINDOW + 2)]

    assert results[: run_local.MAX_RESTARTS_IN_WINDOW].count(None) == 0
    assert results[-1] is None


def test_moderate_cadence_crash_loop_still_gives_up():
    """Regression: an earlier version reset the counter on any run that
    survived 60s (MIN_HEALTHY_UPTIME_SECONDS), which meant a child crashing
    every ~65s would restart forever and never trip the guard. The counter
    must now depend only on crash frequency within the rolling window, not
    on how long any individual run lasted — simulate crashes spaced 65s
    apart (all "healthy" by the old, now-removed standard) and confirm the
    guard still fires once enough of them land inside the window."""
    tracker = run_local._RestartTracker()
    results = []
    for _ in range(run_local.MAX_RESTARTS_IN_WINDOW + 2):
        results.append(tracker.next_backoff_or_give_up())
        tracker.window_start -= 65.0  # simulate 65s of real time passing

    assert None in results


def test_window_expiry_resets_counter():
    tracker = run_local._RestartTracker()
    for _ in range(run_local.MAX_RESTARTS_IN_WINDOW):
        tracker.next_backoff_or_give_up()
    assert tracker.restart_count == run_local.MAX_RESTARTS_IN_WINDOW

    # Simulate the rolling window having elapsed since the first crash in it.
    tracker.window_start -= run_local.RESTART_WINDOW_SECONDS + 1

    backoff = tracker.next_backoff_or_give_up()
    assert backoff == run_local.RESTART_INITIAL_BACKOFF_SECONDS
    assert tracker.restart_count == 1


# -- _interruptible_sleep: shutdown signal must not wait out a long backoff --


def test_interruptible_sleep_returns_early_when_stop_requested():
    """A shutdown signal arriving mid-backoff must be honored within
    _SLEEP_STEP_SECONDS, not only after the full duration — otherwise a
    backoff near RESTART_MAX_BACKOFF_SECONDS (60s) could outlast launchd's
    default 20s ExitTimeOut and get SIGKILLed before cleanup runs."""
    calls = {"n": 0}

    def stop_after_two_checks() -> bool:
        calls["n"] += 1
        return calls["n"] > 2

    start = time.monotonic()
    run_local._interruptible_sleep(30.0, stop_after_two_checks)
    elapsed = time.monotonic() - start

    assert elapsed < 2.0  # nowhere near the full 30s duration
    assert calls["n"] >= 2


def test_interruptible_sleep_with_no_stop_check_sleeps_full_duration():
    start = time.monotonic()
    run_local._interruptible_sleep(0.1, None)
    assert time.monotonic() - start >= 0.1


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
    crashed.wait()  # deterministically wait for it to actually exit
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
        run_local._terminate_all({"healthy": healthy, "crashed_replacement": procs["crashed"]})


def test_check_children_skips_respawn_and_remaining_children_when_stopping(monkeypatch):
    """If a shutdown signal arrives during one child's backoff, that child
    must not be respawned, and no *other* crashed child in the same tick
    should be checked either — main() is about to tear everything down
    anyway, so there's no point starting more processes only to kill them."""
    monkeypatch.setattr(run_local, "RESTART_INITIAL_BACKOFF_SECONDS", 0.01)

    crashed_a = _trivial_exit(1)
    crashed_b = _trivial_exit(1)
    crashed_a.wait()
    crashed_b.wait()
    try:
        procs = {"a": crashed_a, "b": crashed_b}
        started_at = {"a": time.monotonic(), "b": time.monotonic()}
        trackers = {"a": run_local._RestartTracker(), "b": run_local._RestartTracker()}

        spawn_calls = {"n": 0}

        def fake_spawn(module: str) -> subprocess.Popen:
            spawn_calls["n"] += 1
            return _trivial_sleep()

        monkeypatch.setattr(run_local, "_spawn", fake_spawn)

        ok = run_local._check_children(
            procs,
            started_at,
            trackers,
            child_specs=(("a", "unused"), ("b", "unused")),
            stop_requested=lambda: True,
        )

        assert ok is True
        assert spawn_calls["n"] == 0
        assert procs["a"] is crashed_a  # never replaced
        assert procs["b"] is crashed_b  # "b" never even checked
    finally:
        run_local._terminate_all({"a": procs["a"], "b": procs["b"]})


def test_check_children_gives_up_after_repeated_crashes(monkeypatch):
    monkeypatch.setattr(run_local, "RESTART_INITIAL_BACKOFF_SECONDS", 0.01)
    monkeypatch.setattr(run_local, "MAX_RESTARTS_IN_WINDOW", 1)

    def fake_spawn(module: str) -> subprocess.Popen:
        return _trivial_exit(1)

    monkeypatch.setattr(run_local, "_spawn", fake_spawn)

    crashed = _trivial_exit(1)
    crashed.wait()  # deterministically wait for it to actually exit
    procs = {"flaky": crashed}
    started_at = {"flaky": time.monotonic()}
    trackers = {"flaky": run_local._RestartTracker()}

    # First check: restarts once (attempt 1, within MAX_RESTARTS_IN_WINDOW=1).
    ok = run_local._check_children(
        procs, started_at, trackers, child_specs=(("flaky", "unused"),)
    )
    assert ok is True
    procs["flaky"].wait()  # let the replacement (also exit(1)) actually exit

    # Second check: this is attempt 2, over the limit — must give up.
    ok = run_local._check_children(
        procs, started_at, trackers, child_specs=(("flaky", "unused"),)
    )
    assert ok is False


def test_fence_refusal_is_not_respawned_by_child_restart_loop(monkeypatch):
    class RefusedProcess:
        pid = 4321
        returncode = FENCE_REFUSAL_EXIT_CODE

        def poll(self):
            return self.returncode

    spawn_calls = []
    monkeypatch.setattr(run_local, "_spawn", lambda module: spawn_calls.append(module))
    procs = {"telegram_bot": RefusedProcess()}
    started_at = {"telegram_bot": time.monotonic()}
    trackers = {"telegram_bot": run_local._RestartTracker()}

    ok = run_local._check_children(
        procs,
        started_at,
        trackers,
        child_specs=(("telegram_bot", "app.commands.run_telegram_bot"),),
    )

    assert ok is False
    assert spawn_calls == []
