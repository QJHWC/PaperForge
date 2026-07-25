from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .config import PaperForgeConfig
from .models import CompletionGate, ExecutionProfile, WorkflowStatus
from .policy import ExecutionPolicy
from .scientific_memory import _stable_id
from .workflow import InvalidTransition, WorkflowEngine


@dataclass(frozen=True)
class RunHandle:
    run_id: str
    profile: str
    status: str
    checkpoint: str | None
    workspace: str
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PaperForgeService:
    """Single public facade used by the CLI, frontend and compatibility wrappers."""

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.workflow = WorkflowEngine(self.workspace)
        self.memory = self.workflow.memory

    @staticmethod
    def _handle(state: Mapping[str, Any]) -> RunHandle:
        return RunHandle(
            run_id=str(state["id"]),
            profile=str(state["profile"]),
            status=str(state["status"]),
            checkpoint=state.get("checkpoint"),
            workspace=str(state["workspace"]),
            metadata=dict(state.get("metadata") or {}),
        )

    def run(
        self,
        *,
        profile: str | ExecutionProfile,
        legacy_mode: str | None = None,
        proposal_id: str | None = None,
        idempotency_key: str | None = None,
        inputs: Mapping[str, Any] | None = None,
    ) -> RunHandle:
        normalized = ExecutionProfile(profile)
        policy = ExecutionPolicy(normalized)
        effective_proposal_id = proposal_id
        if normalized is ExecutionProfile.FULL and not effective_proposal_id:
            effective_proposal_id = _stable_id(
                "proposal",
                str(self.workspace),
                idempotency_key or "pending",
            )
        metadata = {
            "legacy_mode": legacy_mode,
            "inputs": dict(inputs or {}),
            "proposal_id": effective_proposal_id,
            "runtime": "paperforge-v3",
        }
        run_id = self.workflow.create(
            normalized,
            metadata=metadata,
            idempotency_key=idempotency_key,
        )
        self.workflow.transition(
            run_id,
            WorkflowStatus.RUNNING,
            checkpoint="policy",
            detail=f"execution profile {normalized.value} activated",
            idempotency_key=f"{run_id}:policy",
        )

        context_paths = [
            Path(value)
            for key, value in dict(inputs or {}).items()
            if key.endswith(("_path", "_file")) and isinstance(value, str | Path)
        ]
        policy.validate_context_paths(context_paths)

        if normalized is ExecutionProfile.FULL:
            try:
                self.workflow.require_approval(str(effective_proposal_id))
            except PermissionError:
                state = self.workflow.transition(
                    run_id,
                    WorkflowStatus.AWAITING_APPROVAL,
                    checkpoint="proposal",
                    detail=f"full profile requires approved proposal {effective_proposal_id}",
                    idempotency_key=f"{run_id}:awaiting-approval",
                )
                return self._handle(state)

        state = self._execute_runtime(run_id)
        return self._handle(state)

    def _execute_runtime(self, run_id: str) -> dict[str, Any]:
        from .runtime import ResearchOSRuntime

        state = self.workflow.get(run_id)
        metadata = dict(state.get("metadata") or {})
        runtime = ResearchOSRuntime(
            self.workspace,
            profile=str(state["profile"]),
            memory=self.memory,
        )
        report = runtime.execute(
            run_id=run_id,
            legacy_mode=metadata.get("legacy_mode"),
            inputs=metadata.get("inputs"),
        )
        return self.workflow.record_checkpoint(
            run_id,
            checkpoint="runtime",
            detail="concrete Research OS agents executed",
            metadata_updates={
                "runtime_executed": True,
                "runtime_report": report.report_path,
                "runtime_manifest": report.manifest_path,
                "provider_status": report.provider_status,
                "completed_roles": list(report.completed_roles),
                "blocked_roles": list(report.blocked_roles),
            },
            idempotency_key=f"{run_id}:runtime",
        )

    def approve(
        self,
        proposal_id: str,
        *,
        approved_by: str = "local-owner",
        scope: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        approval_id = self.workflow.approve(
            proposal_id,
            approved_by=approved_by,
            scope=scope,
        )
        return {"approval_id": approval_id, "proposal_id": proposal_id, "status": "APPROVED"}

    def status(self, run_id: str | None = None) -> RunHandle:
        state = self.workflow.get(run_id) if run_id else self.workflow.latest()
        return self._handle(state)

    def resume(self, run_id: str | None = None) -> RunHandle:
        state = self.workflow.get(run_id) if run_id else self.workflow.latest()
        current = WorkflowStatus(state["status"])
        if current is WorkflowStatus.AWAITING_APPROVAL:
            metadata = state.get("metadata", {})
            proposal_id = metadata.get("proposal_id")
            if not proposal_id:
                raise PermissionError("workflow has no proposal identifier")
            self.workflow.require_approval(str(proposal_id))
        resumed = self.workflow.transition(
            str(state["id"]),
            WorkflowStatus.RUNNING,
            checkpoint="resumed",
            detail="workflow resumed from persisted state",
            idempotency_key=f"{state['id']}:resume:{state.get('version', 0)}",
        )
        return self._handle(self._execute_runtime(str(resumed["id"])))

    def publish(
        self,
        *,
        template: str,
        main_tex: str | Path | None = None,
    ) -> dict[str, Any]:
        from .publication import PublicationEngine

        engine = PublicationEngine()
        result = engine.publish(
            self.workspace,
            template=template,
            main_tex=str(main_tex) if main_tex else "main.tex",
            scientific_memory=self.memory,
        )
        payload = result.to_dict() if hasattr(result, "to_dict") else asdict(result)
        if not payload.get("success"):
            raise RuntimeError("publication pipeline did not pass")
        return payload

    def release(
        self,
        *,
        run_id: str | None = None,
        gate: CompletionGate | None = None,
    ) -> RunHandle:
        from .release import ReleaseVerifier

        state = self.workflow.get(run_id) if run_id else self.workflow.latest()
        if gate is not None:
            raise InvalidTransition("caller-supplied release gates are not accepted")
        verifier = ReleaseVerifier(self.workspace, memory=self.memory)
        gate = verifier.verify()
        verifier.write_report(gate)
        completed = self.workflow.transition(
            str(state["id"]),
            WorkflowStatus.COMPLETED,
            checkpoint="release",
            gate=gate,
            detail="all release gates verified",
            idempotency_key=f"{state['id']}:release",
        )
        return self._handle(completed)


def load_service(
    workspace: str | Path,
    *,
    profile: str | ExecutionProfile | None = None,
) -> tuple[PaperForgeConfig, PaperForgeService]:
    config = PaperForgeConfig.load(workspace, profile=profile)
    return config, PaperForgeService(config.workspace)
