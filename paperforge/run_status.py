from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

from .models import WorkflowStatus

PROCESS_STATES = (
    "RUNNING",
    "PAUSED",
    "EXITED_SUCCESS",
    "EXITED_FAILURE",
    "STOPPING",
    "UNKNOWN_NOT_ATTACHED",
)

# Deprecated aliases from the old process vocabulary (exit code collapsed).
_DEPRECATED_PROCESS_ALIASES = {
    "attached_running": "RUNNING",
    "attached_paused": "PAUSED",
    "exited": "EXITED_SUCCESS",
    "stopping": "STOPPING",
    "unknown_not_attached": "UNKNOWN_NOT_ATTACHED",
    "running": "RUNNING",
    "paused": "PAUSED",
    "completed": "EXITED_SUCCESS",
    "failed": "EXITED_FAILURE",
}

_HUMAN_GATES = frozenset({
    WorkflowStatus.AWAITING_APPROVAL.value,
    WorkflowStatus.AUTH_BLOCKED.value,
    WorkflowStatus.PAUSED.value,
    WorkflowStatus.INTERRUPTED.value,
})

RELATIONS = (
    "consistent_active",
    "consistent_quiescent",
    "process_unknown",
    "suspicious",
)


@dataclass(frozen=True)
class UnifiedRunStatus:
    """Read-only projection. Never a third state database.

    workflow_state is the only business truth (Workflow DB / domain artifacts).
    process_state is operating-system supervision truth (ProcessManager only).
    process_domain_relation classifies the pair; process_discrepancy is only
    set for genuinely suspicious combinations, never for legitimate quiescence.
    """

    run_id: str
    workflow_state: str
    process_state: str = "UNKNOWN_NOT_ATTACHED"
    process_domain_relation: str = "process_unknown"
    process_discrepancy: str = ""
    pending_human_gate: str = ""
    stage: str = ""
    last_checkpoint: str = ""
    checkpoint: str = ""
    failure_summary: str = ""
    artifact_ids: tuple[str, ...] = ()
    review_ids: tuple[str, ...] = ()
    updated_at: str = ""
    # Deprecated compat: mirrors workflow_state, never process state.
    status: str = ""
    release_assessment: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.process_state not in PROCESS_STATES:
            raise ValueError(f"unknown process_state: {self.process_state}")
        if self.process_domain_relation not in RELATIONS:
            raise ValueError(f"unknown relation: {self.process_domain_relation}")
        object.__setattr__(self, "artifact_ids", tuple(self.artifact_ids or ()))
        object.__setattr__(self, "review_ids", tuple(self.review_ids or ()))
        if not self.status:
            object.__setattr__(self, "status", self.workflow_state)
        if not self.checkpoint:
            object.__setattr__(self, "checkpoint", self.last_checkpoint)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["artifact_ids"] = list(payload["artifact_ids"])
        payload["review_ids"] = list(payload["review_ids"])
        return payload


def _normalize_process(payload: Mapping[str, Any] | None) -> str:
    if not isinstance(payload, Mapping) or not payload:
        return "UNKNOWN_NOT_ATTACHED"
    raw = str(payload.get("process_state") or payload.get("status") or "").strip()
    upper = raw.upper()
    if upper in PROCESS_STATES:
        # EXITED without code defaults to success only when the payload says so;
        # callers should prefer passing exit_code (see below).
        return upper
    lowered = raw.strip().lower()
    if lowered in _DEPRECATED_PROCESS_ALIASES:
        base = _DEPRECATED_PROCESS_ALIASES[lowered]
        if base == "EXITED_SUCCESS" and str(payload.get("status") or "").strip().lower() == "failed":
            return "EXITED_FAILURE"
        exit_code = payload.get("exit_code")
        if lowered in {"completed", "exited", "failed"} and isinstance(exit_code, int):
            return "EXITED_SUCCESS" if exit_code == 0 else "EXITED_FAILURE"
        return base
    exit_code = payload.get("exit_code")
    if isinstance(exit_code, int):
        return "EXITED_SUCCESS" if exit_code == 0 else "EXITED_FAILURE"
    return "UNKNOWN_NOT_ATTACHED"


def _classify_relation(workflow_state: str, process_state: str) -> tuple[str, str]:
    """Return (relation, discrepancy). Legitimate quiescence is never a mismatch."""
    quiescent = {
        WorkflowStatus.AWAITING_APPROVAL.value,
        WorkflowStatus.PAUSED.value,
        WorkflowStatus.INTERRUPTED.value,
        WorkflowStatus.AUTH_BLOCKED.value,
        WorkflowStatus.FAILED.value,
        WorkflowStatus.CANCELLED.value,
        WorkflowStatus.COMPLETED.value,
    }
    if process_state == "UNKNOWN_NOT_ATTACHED":
        return "process_unknown", ""
    if workflow_state == WorkflowStatus.RUNNING.value and process_state == "RUNNING":
        return "consistent_active", ""
    if workflow_state == WorkflowStatus.PAUSED.value and process_state == "PAUSED":
        return "consistent_active", ""
    if workflow_state in quiescent and process_state in {"EXITED_SUCCESS", "EXITED_FAILURE"}:
        return "consistent_quiescent", ""
    if workflow_state == WorkflowStatus.FAILED.value and process_state == "EXITED_FAILURE":
        return "consistent_quiescent", ""
    if workflow_state == WorkflowStatus.RUNNING.value and process_state in {
        "EXITED_SUCCESS", "EXITED_FAILURE"}:
        return "suspicious", "PROCESS_EXITED_WHILE_WORKFLOW_RUNNING"
    if workflow_state == WorkflowStatus.COMPLETED.value and process_state in {
        "RUNNING", "PAUSED"}:
        return "suspicious", "PROCESS_ACTIVE_WHILE_WORKFLOW_COMPLETED"
    return "suspicious", f"UNEXPECTED_PROCESS_DOMAIN_PAIR: {workflow_state}/{process_state}"


def build_unified_status(
    workflow_state: Mapping[str, Any],
    process_payload: Mapping[str, Any] | None = None,
    *,
    release_assessment: Mapping[str, Any] | None = None,
) -> UnifiedRunStatus:
    """Build the shared projection. Read-only: never writes domain or process state."""
    run_id = str(workflow_state.get("id") or "")
    state = str(workflow_state.get("status") or WorkflowStatus.READY.value)
    try:
        WorkflowStatus(state)
    except ValueError:
        state = WorkflowStatus.READY.value
    process_state = _normalize_process(process_payload)
    metadata = dict(workflow_state.get("metadata") or {})
    relation, discrepancy = _classify_relation(state, process_state)
    pending = state if state in _HUMAN_GATES else ""
    failure = ""
    if state in {WorkflowStatus.FAILED.value, WorkflowStatus.AUTH_BLOCKED.value}:
        failure = str(workflow_state.get("error_code") or metadata.get("detail") or state)
    artifacts = metadata.get("runtime_manifest") or metadata.get("runtime_report")
    artifact_ids = tuple(a for a in ([artifacts] if isinstance(artifacts, str) else (artifacts or [])) if a)
    checkpoint = str(workflow_state.get("checkpoint") or "")
    return UnifiedRunStatus(
        run_id=run_id,
        workflow_state=state,
        process_state=process_state,
        process_domain_relation=relation,
        process_discrepancy=discrepancy,
        pending_human_gate=pending,
        stage=checkpoint,
        last_checkpoint=checkpoint,
        checkpoint=checkpoint,
        failure_summary=failure,
        artifact_ids=artifact_ids,
        review_ids=(),
        updated_at=str(workflow_state.get("updated_at") or ""),
        status=state,
        release_assessment={
            **{key: release_assessment[key] for key in (
                "report_id", "overall_eligibility", "currently_valid", "authorization_state", "release_state"
            ) if key in release_assessment},
            "authoritative": False,
        } if release_assessment else {},
    )


__all__ = ["PROCESS_STATES", "RELATIONS", "UnifiedRunStatus", "build_unified_status"]
