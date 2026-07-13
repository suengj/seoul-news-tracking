from __future__ import annotations

import pytest

from app.process_lock import SingleInstanceLock


def test_lock_can_be_acquired_and_released(tmp_path):
    lock = SingleInstanceLock(tmp_path / "test.lock")
    lock.acquire()
    lock.release()


def test_second_acquire_fails_while_first_holds_lock(tmp_path):
    lock_path = tmp_path / "test.lock"
    first = SingleInstanceLock(lock_path)
    first.acquire()

    second = SingleInstanceLock(lock_path)
    with pytest.raises(RuntimeError):
        second.acquire()

    first.release()


def test_lock_available_again_after_release(tmp_path):
    lock_path = tmp_path / "test.lock"
    first = SingleInstanceLock(lock_path)
    first.acquire()
    first.release()

    second = SingleInstanceLock(lock_path)
    second.acquire()  # must not raise
    second.release()


def test_lock_as_context_manager(tmp_path):
    lock_path = tmp_path / "test.lock"
    with SingleInstanceLock(lock_path):
        blocked = SingleInstanceLock(lock_path)
        with pytest.raises(RuntimeError):
            blocked.acquire()
    # released on context exit
    again = SingleInstanceLock(lock_path)
    again.acquire()
    again.release()


def test_lock_creates_parent_directory(tmp_path):
    lock_path = tmp_path / "nested" / "dir" / "test.lock"
    lock = SingleInstanceLock(lock_path)
    lock.acquire()
    assert lock_path.exists()
    lock.release()
