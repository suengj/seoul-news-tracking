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
