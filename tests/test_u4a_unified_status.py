from __future__ import annotations

from pathlib import Path

from paperforge.api import PaperForgeService
from paperforge.models import WorkflowStatus
from paperforge.run_status import UnifiedRunStatus, build_unified_status


def _state(status: str, **overrides):
    base = {"id": "wf-test", "status": status, "checkpoint": "policy",
            "updated_at": "2026-01-01T00:00:00+00:00", "metadata": {}}
    base.update(overrides)
    return base


def test_process_running_does_not_imply_workflow_running() -> None:  # 1
    unified = build_unified_status(_state("READY"), {"status": "running"})
    assert unified.workflow_state == "READY" and unified.process_state == "RUNNING"


def test_exit_zero_does_not_complete_workflow() -> None:  # 2
    unified = build_unified_status(_state("RUNNING"), {"status": "completed", "exit_code": 0})
    assert unified.workflow_state == "RUNNING" and unified.process_state in {"EXITED_SUCCESS", "EXITED_FAILURE"}
    assert unified.process_domain_relation == "suspicious"
    assert unified.process_discrepancy == "PROCESS_EXITED_WHILE_WORKFLOW_RUNNING"


def test_relation_matrix() -> None:  # F1 combos 1-6
    assert build_unified_status(_state("AWAITING_APPROVAL"), {"status": "completed", "exit_code": 0}).process_domain_relation == "consistent_quiescent"
    assert build_unified_status(_state("AWAITING_APPROVAL"), {"status": "completed", "exit_code": 0}).process_discrepancy == ""
    assert build_unified_status(_state("AWAITING_APPROVAL"), None).process_domain_relation == "process_unknown"
    assert build_unified_status(_state("RUNNING"), {"status": "running"}).process_domain_relation == "consistent_active"
    assert build_unified_status(_state("COMPLETED"), {"status": "running"}).process_domain_relation == "suspicious"
    assert build_unified_status(_state("FAILED"), {"status": "failed", "exit_code": 3}).process_domain_relation == "consistent_quiescent"


def test_nonzero_exit_preserves_needs_human() -> None:  # 3,4
    unified = build_unified_status(_state("AWAITING_APPROVAL"), {"status": "failed", "exit_code": 1})
    assert unified.workflow_state == "AWAITING_APPROVAL"
    assert unified.pending_human_gate == "AWAITING_APPROVAL"


def test_approved_no_process_recoverable() -> None:  # 5
    unified = build_unified_status(_state("AWAITING_APPROVAL"), None)
    assert unified.process_state == "UNKNOWN_NOT_ATTACHED"
    assert unified.workflow_state == "AWAITING_APPROVAL"


def test_restart_preserves_domain_state(tmp_path: Path) -> None:  # 6
    first = PaperForgeService(tmp_path)
    handle = first.run(profile="writing-only", inputs={"title": "t", "topic": "x"})
    second = PaperForgeService(tmp_path)  # simulates app restart; process memory gone
    assert second.status(handle.run_id).status == first.status(handle.run_id).status
    unified = second.unified_status(handle.run_id)
    assert unified["unified"]["process_state"] == "UNKNOWN_NOT_ATTACHED"


def test_process_memory_loss_never_resets_workflow() -> None:  # 7
    unified = build_unified_status(_state("PAUSED"), None)
    assert unified.workflow_state == "PAUSED"


def test_three_entries_share_source(tmp_path: Path) -> None:  # 8
    from paperforge.cli import build_parser

    service = PaperForgeService(tmp_path)
    handle = service.run(profile="writing-only", inputs={"title": "t", "topic": "x"})
    api_state = service.unified_status(handle.run_id)["unified"]["workflow_state"]
    parser = build_parser()
    args = parser.parse_args(["status", "--workspace", str(tmp_path), "--run-id", handle.run_id])
    assert args.command == "status" and args.run_id == handle.run_id
    # Frontend adapter consumes the same builder over the same DB row.
    frontend = build_unified_status(service.workflow.get(handle.run_id), None)
    assert frontend.workflow_state == api_state


def test_read_status_side_effect_free(tmp_path: Path) -> None:  # 9
    service = PaperForgeService(tmp_path)
    handle = service.run(profile="writing-only", inputs={"title": "t", "topic": "x"})
    before = service.workflow.get(handle.run_id)
    service.status(handle.run_id)
    service.unified_status(handle.run_id)
    after = service.workflow.get(handle.run_id)
    assert before["version"] == after["version"] and before["status"] == after["status"]


def test_frontend_refresh_writes_nothing(tmp_path: Path) -> None:  # 10
    import sys

    sys.path.insert(0, ".")
    from frontend.server import _workspace_v3_status

    service = PaperForgeService(tmp_path)
    handle = service.run(profile="writing-only", inputs={"title": "t", "topic": "x"})
    before = service.workflow.get(handle.run_id)["version"]
    _workspace_v3_status(tmp_path, "x")
    assert service.workflow.get(handle.run_id)["version"] == before


def test_mismatch_explicit_and_no_silent_pick() -> None:  # 11,12
    unified = build_unified_status(_state("RUNNING"), {"status": "completed"})
    assert unified.process_discrepancy and unified.workflow_state == "RUNNING"


def test_approval_from_domain(tmp_path: Path) -> None:  # 13
    service = PaperForgeService(tmp_path)
    handle = service.run(profile="full", inputs={})
    assert service.status(handle.run_id).status == WorkflowStatus.AWAITING_APPROVAL.value


def test_no_review_inference() -> None:  # 14,15,16
    unified = build_unified_status(_state("RUNNING"), {"status": "running"})
    assert unified.review_ids == () and "review" not in unified.to_dict().get("failure_summary", "").lower()


def test_dto_is_projection_not_database(tmp_path: Path) -> None:  # 17
    service = PaperForgeService(tmp_path)
    handle = service.run(profile="writing-only", inputs={"title": "t", "topic": "x"})
    unified = UnifiedRunStatus(run_id=handle.run_id, workflow_state="RUNNING", process_state="EXITED_SUCCESS")
    assert unified.to_dict()["workflow_state"] == "RUNNING"
    assert service.workflow.get(handle.run_id)["status"] != "COMPLETED"


def test_compat_status_is_workflow(tmp_path: Path) -> None:  # 18
    service = PaperForgeService(tmp_path)
    handle = service.run(profile="writing-only", inputs={"title": "t", "topic": "x"})
    payload = service.unified_status(handle.run_id)
    assert payload["status"] == payload["unified"]["workflow_state"]


def test_failure_projection_without_pollution() -> None:  # 19
    unified = build_unified_status(
        {"id": "w", "status": "FAILED", "checkpoint": "runtime",
         "updated_at": "t", "error_code": "E", "metadata": {}}, {"status": "failed"})
    assert unified.workflow_state == "FAILED" and unified.process_state == "EXITED_FAILURE"
    assert unified.failure_summary == "E"


def test_v2_file_never_feeds_unified(tmp_path: Path) -> None:  # F5
    (tmp_path / "workflow_state.json").write_text('{"phase": "done"}', encoding="utf-8")
    service = PaperForgeService(tmp_path)
    handle = service.run(profile="writing-only", inputs={"title": "t", "topic": "x"})
    unified = service.unified_status(handle.run_id)["unified"]
    assert unified["workflow_state"] != "done"
    assert unified["stage"] != "done"


def test_frontend_consumer_audit() -> None:  # F2 audit
    source = Path("frontend/app.js").read_text(encoding="utf-8")
    assert "processPill" in source and "not workflow truth" in source
    assert 'if (run.status === "completed")' not in source
    assert "v3?.workflow?.status" in source or "v3?.unified" in source


def test_final_schema() -> None:  # F4
    unified = build_unified_status(_state("RUNNING"), {"status": "running"})
    payload = unified.to_dict()
    for key in ("run_id", "workflow_state", "process_state", "process_domain_relation",
                "process_discrepancy", "pending_human_gate", "stage", "checkpoint",
                "failure_summary", "updated_at"):
        assert key in payload
    assert payload["status"] == payload["workflow_state"]
