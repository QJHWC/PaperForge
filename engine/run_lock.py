"""Process-level exclusive lock for PaperForge workspaces.

Prevents concurrent processes from writing to the same workspace
(e.g. two training sweeps or a sync + writeup running in parallel).
Uses POSIX fcntl advisory locking; on platforms without fcntl support,
locking is unavailable and callers will receive a RuntimeError.
"""

from __future__ import annotations

import time
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, TextIO

try:
    import fcntl
except Exception as exc:
    fcntl = None  # type: ignore[assignment]
    _FCNTL_IMPORT_ERROR = exc
else:
    _FCNTL_IMPORT_ERROR = None


_REENTRANT_GUARD = threading.RLock()
_REENTRANT_LOCKS: dict[str, dict[str, object]] = {}


class RunLockTimeoutError(TimeoutError):
    """Raised when the workspace lock cannot be acquired within timeout."""


def acquire_run_lock(
    run_dir: Path,
    timeout: int = 30,
    poll_interval: float = 0.2,
    verbose: bool = True,
) -> TextIO:
    """Acquire an exclusive lock on *run_dir*.

    The caller must keep the returned file handle alive until the
    protected operation completes; closing the handle releases the lock.
    """
    if fcntl is None:
        raise RuntimeError(f"fcntl unavailable on this platform: {_FCNTL_IMPORT_ERROR}")

    run_dir = run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    lock_path = (run_dir / ".run.lock").resolve()

    with _REENTRANT_GUARD:
        existing = _REENTRANT_LOCKS.get(str(lock_path))
        if existing is not None:
            existing["count"] = int(existing["count"]) + 1
            return existing["fp"]  # type: ignore[return-value]

    lock_path.touch(exist_ok=True)
    fp = lock_path.open("w", encoding="utf-8")

    start = time.monotonic()
    waiting_printed = False
    while True:
        try:
            fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with _REENTRANT_GUARD:
                _REENTRANT_LOCKS[str(lock_path)] = {
                    "fp": fp,
                    "count": 1,
                }
            return fp
        except BlockingIOError:
            elapsed = time.monotonic() - start
            if timeout >= 0 and elapsed >= float(timeout):
                fp.close()
                raise RunLockTimeoutError(
                    f"Timeout waiting for run lock: {lock_path} (timeout={timeout}s)"
                )
            if verbose and not waiting_printed:
                print(f"[LOCK] waiting for run lock: {lock_path} (timeout={timeout}s)")
                waiting_printed = True
            time.sleep(float(poll_interval))


def release_run_lock(lock_fp: TextIO) -> None:
    if fcntl is None:
        return
    lock_path = Path(getattr(lock_fp, "name", "")).resolve()
    with _REENTRANT_GUARD:
        existing = _REENTRANT_LOCKS.get(str(lock_path))
        if existing is not None and existing.get("fp") is lock_fp:
            remaining = int(existing["count"]) - 1
            if remaining > 0:
                existing["count"] = remaining
                return
            _REENTRANT_LOCKS.pop(str(lock_path), None)
    try:
        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)
    finally:
        lock_fp.close()


@contextmanager
def run_lock(
    run_dir: Path,
    timeout: int = 30,
    poll_interval: float = 0.2,
    verbose: bool = True,
) -> Iterator[TextIO]:
    """Context manager for workspace-level exclusive locking."""
    lock_fp = acquire_run_lock(
        run_dir, timeout=timeout, poll_interval=poll_interval, verbose=verbose
    )
    try:
        yield lock_fp
    finally:
        release_run_lock(lock_fp)
