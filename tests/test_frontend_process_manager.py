from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

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


def test_start_run_builds_only_unified_cli_commands(tmp_path, monkeypatch) -> None:
    root = tmp_path / "repo"
    results = root / "results"
    workspace = results / "paper_writer" / "demo"
    workspace.mkdir(parents=True)
    manager = ProcessManager(root=root, results_dir=results)
    captured = {}

    def fake_launch_process(**kwargs):
        captured.update(kwargs)
        return kwargs

    monkeypatch.setattr(manager, "launch_process", fake_launch_process)
    manager.start_run(
        {
            "entry": "run",
            "profile": "research",
            "legacy_mode": "research_partner",
            "workspace_rel": "paper_writer/demo",
        }
    )

    assert captured["command"] == [
        sys.executable,
        "-m",
        "paperforge",
        "run",
        "--profile",
        "research",
        "--workspace",
        str(workspace.resolve()),
        "--legacy-mode",
        "research_partner",
    ]


@pytest.mark.parametrize("key", ["command", "args", "shell", "env", "cwd", "executable"])
def test_start_run_rejects_browser_command_overrides(tmp_path, key) -> None:
    root = tmp_path / "repo"
    workspace = root / "results" / "paper_writer" / "demo"
    workspace.mkdir(parents=True)
    manager = ProcessManager(root=root, results_dir=root / "results")

    with pytest.raises(ValueError, match="forbidden"):
        manager.start_run(
            {
                "entry": "run",
                "profile": "full",
                "workspace_rel": "paper_writer/demo",
                key: "attacker-controlled",
            }
        )


def test_workspace_symlink_escape_is_rejected(tmp_path) -> None:
    root = tmp_path / "repo"
    results = root / "results"
    outside = tmp_path / "outside"
    outside.mkdir()
    results.mkdir(parents=True)
    (results / "escape").symlink_to(outside, target_is_directory=True)
    manager = ProcessManager(root=root, results_dir=results)

    with pytest.raises(ValueError, match="escapes"):
        manager.start_run(
            {
                "entry": "run",
                "profile": "full",
                "workspace_rel": "escape",
            }
        )


def test_read_log_ignores_poisoned_metadata_log_path(tmp_path) -> None:
    root = tmp_path / "repo"
    results = root / "results"
    workspace = results / "paper_writer" / "demo"
    run_dir = workspace / "artifacts" / "frontend_runs"
    run_dir.mkdir(parents=True)
    secret = tmp_path / "secret.txt"
    secret.write_text("must not leak", encoding="utf-8")
    (run_dir / "run_test.json").write_text(
        json.dumps(
            {
                "run_id": "run_test",
                "status": "completed",
                "started_at": "2026-07-25T00:00:00",
                "log_path": str(secret),
            }
        ),
        encoding="utf-8",
    )
    manager = ProcessManager(root=root, results_dir=results)

    payload = manager.read_log("paper_writer/demo", "run_test")
    assert payload["text"] == ""
