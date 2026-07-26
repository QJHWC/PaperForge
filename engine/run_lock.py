"""Process-level exclusive lock for PaperForge workspaces.

Prevents concurrent processes from writing to the same workspace
(e.g. two training sweeps or a sync + writeup running in parallel).
Uses POSIX ``flock`` or the Windows CRT byte-range lock.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO, TypedDict

try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:
    msvcrt = None  # type: ignore[assignment]


_REENTRANT_GUARD = threading.RLock()


class _HeldLock(TypedDict):
    fp: TextIO
    count: int


_REENTRANT_LOCKS: dict[str, _HeldLock] = {}


class RunLockTimeoutError(TimeoutError):
    """Raised when the workspace lock cannot be acquired within timeout."""


def _lock_nonblocking(lock_fp: TextIO) -> None:
    lock_fp.seek(0)
    if fcntl is not None:
        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    if msvcrt is not None:
        msvcrt.locking(lock_fp.fileno(), msvcrt.LK_NBLCK, 1)
        return
    raise RuntimeError("no supported process lock implementation is available")


def _unlock(lock_fp: TextIO) -> None:
    lock_fp.seek(0)
    if fcntl is not None:
        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)
    elif msvcrt is not None:
        msvcrt.locking(lock_fp.fileno(), msvcrt.LK_UNLCK, 1)


def _is_lock_contention(exc: OSError) -> bool:
    if isinstance(exc, BlockingIOError):
        return True
    return msvcrt is not None and exc.errno in {13, 33, 36}


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
    if fcntl is None and msvcrt is None:
        raise RuntimeError("no supported process lock implementation is available")

    run_dir = run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    lock_path = (run_dir / ".run.lock").resolve()

    with _REENTRANT_GUARD:
        existing = _REENTRANT_LOCKS.get(str(lock_path))
        if existing is not None:
            existing["count"] = int(existing["count"]) + 1
            return existing["fp"]

    lock_path.touch(exist_ok=True)
    fp = lock_path.open("a+", encoding="utf-8")
    if lock_path.stat().st_size == 0:
        fp.write("\0")
        fp.flush()

    start = time.monotonic()
    waiting_printed = False
    while True:
        try:
            _lock_nonblocking(fp)
            with _REENTRANT_GUARD:
                _REENTRANT_LOCKS[str(lock_path)] = {
                    "fp": fp,
                    "count": 1,
                }
            return fp
        except OSError as exc:
            if not _is_lock_contention(exc):
                fp.close()
                raise
            elapsed = time.monotonic() - start
            if timeout >= 0 and elapsed >= float(timeout):
                fp.close()
                raise RunLockTimeoutError(
                    f"Timeout waiting for run lock: {lock_path} (timeout={timeout}s)"
                ) from None
            if verbose and not waiting_printed:
                print(f"[LOCK] waiting for run lock: {lock_path} (timeout={timeout}s)")
                waiting_printed = True
            time.sleep(float(poll_interval))


def release_run_lock(lock_fp: TextIO) -> None:
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
        _unlock(lock_fp)
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
