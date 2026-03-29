from __future__ import annotations

import multiprocessing as mp
import tempfile
import time
from pathlib import Path
import unittest

from engine.run_lock import RunLockTimeoutError, run_lock


def _hold_lock(path: str, queue: mp.Queue[str]) -> None:
    with run_lock(Path(path), timeout=2, poll_interval=0.05, verbose=False):
        queue.put("locked")
        time.sleep(0.8)


class RunLockTest(unittest.TestCase):
    def test_reentrant_lock_in_same_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            with run_lock(workspace, timeout=1, poll_interval=0.05, verbose=False):
                with run_lock(workspace, timeout=1, poll_interval=0.05, verbose=False):
                    self.assertTrue((workspace / ".run.lock").exists())

    def test_lock_blocks_second_writer(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            queue: mp.Queue[str] = mp.Queue()
            proc = mp.Process(target=_hold_lock, args=(str(workspace), queue))
            proc.start()
            self.assertEqual(queue.get(timeout=2), "locked")

            with self.assertRaises(RunLockTimeoutError):
                with run_lock(workspace, timeout=0.1, poll_interval=0.02, verbose=False):
                    pass

            proc.join(timeout=5)
            self.assertFalse(proc.is_alive())

            with run_lock(workspace, timeout=1, poll_interval=0.05, verbose=False):
                self.assertTrue((workspace / ".run.lock").exists())


if __name__ == "__main__":
    unittest.main()
