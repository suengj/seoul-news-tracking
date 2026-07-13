"""A small POSIX advisory file lock used to refuse starting a duplicate local worker."""

from __future__ import annotations

from pathlib import Path


class SingleInstanceLock:
    """Refuses to acquire if another process already holds the lock on this path."""

    def __init__(self, lock_path: Path):
        self.lock_path = lock_path
        self._fh = None

    def acquire(self) -> None:
        import fcntl

        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.lock_path, "w")
        try:
            fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._fh.close()
            self._fh = None
            raise RuntimeError(
                f"another process appears to already be running (lock held on {self.lock_path})"
            ) from exc

    def release(self) -> None:
        import fcntl

        if self._fh is not None:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "SingleInstanceLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()
