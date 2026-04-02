from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from frontend.process_manager import ProcessManager


def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition not met before timeout")


def test_launch_process_persists_log_and_metadata(tmp_path) -> None:
    root = tmp_path / "repo"
    results = root / "results"
    workspace = results / "paper_writer" / "demo"
    workspace.mkdir(parents=True)
    manager = ProcessManager(root=root, results_dir=results)

    result = manager.launch_process(
        entry="mvp",
        command=[sys.executable, "-c", "print('hello from process')"],
        workspace_rel="paper_writer/demo",
        env=os.environ.copy(),
        details={"phase": "refine"},
    )

    _wait_until(lambda: manager.list_run_records("paper_writer/demo")[0]["status"] in {"completed", "failed"})
    records = manager.list_run_records("paper_writer/demo")
    assert records
    record = records[0]
    assert record["run_id"] == result["run_id"]
    assert record["status"] == "completed"
    log_path = Path(record["log_path"])
    assert log_path.exists()
    assert "hello from process" in log_path.read_text(encoding="utf-8")


def test_pause_resume_stop_lifecycle(tmp_path) -> None:
    if os.name != "posix":
        return

    root = tmp_path / "repo"
    results = root / "results"
    workspace = results / "paper_writer" / "demo"
    workspace.mkdir(parents=True)
    manager = ProcessManager(root=root, results_dir=results)

    result = manager.launch_process(
        entry="mvp",
        command=[sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(30)"],
        workspace_rel="paper_writer/demo",
        env=os.environ.copy(),
        details={"phase": "refine"},
    )

    _wait_until(lambda: Path(manager.list_run_records("paper_writer/demo")[0]["log_path"]).exists())
    paused = manager.pause_run(result["run_id"])
    assert paused["status"] == "paused"
    resumed = manager.resume_run(result["run_id"])
    assert resumed["status"] == "running"
    manager.stop_run(result["run_id"])
    _wait_until(lambda: manager.list_run_records("paper_writer/demo")[0]["status"] in {"failed", "completed"})
