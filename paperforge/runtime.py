from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from engine.secret_redaction import redact_structure

from .agents import (
    AgentRegistry,
    AgentRequest,
    AgentResult,
    AgentResultStatus,
    AgentRole,
    AnalysisAgent,
    CodeAgent,
    ComputeAgent,
    ExperimentAgent,
    PaperAgent,
    PersistentTraceStore,
    ReleaseAgent,
    ResearchAgent,
    ReviewerAgent,
    VisualizationAgent,
)
from .artifacts import ArtifactStore, sha256_file
from .experiments import ExperimentManager
from .models import ExecutionProfile, utc_now
from .policy import Action, ExecutionPolicy, PolicyViolation
from .provider import ProviderRegistry
from .scientific_memory import ScientificMemory

_SKIPPED_ROOTS = {
    ".git",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    ".venv-writeup",
    "__pycache__",
    "output",
    "results",
}

_ROLE_ACTION = {
    AgentRole.RESEARCH: Action.EVIDENCE_READ,
    AgentRole.EXPERIMENT: Action.EXPERIMENT_STATIC,
    AgentRole.CODE: Action.CODE_PATCH,
    AgentRole.COMPUTE: Action.LOCAL_EXECUTE,
    AgentRole.ANALYSIS: Action.EVIDENCE_READ,
    AgentRole.VISUALIZATION: Action.PUBLICATION_VISUAL,
    AgentRole.PAPER: Action.DRAFT_EDIT,
    AgentRole.REVIEWER: Action.EVIDENCE_READ,
    AgentRole.RELEASE: Action.GITHUB_WRITE,
}

_PROFILE_ROLES = {
    ExecutionProfile.WRITING_ONLY: (
        AgentRole.PAPER,
        AgentRole.REVIEWER,
    ),
    ExecutionProfile.RESEARCH: (
        AgentRole.RESEARCH,
        AgentRole.ANALYSIS,
        AgentRole.PAPER,
        AgentRole.REVIEWER,
    ),
    ExecutionProfile.FULL: tuple(AgentRole),
}

_LEGACY_ROLES = {
    "writeup": (AgentRole.PAPER, AgentRole.REVIEWER),
    "research_partner": (
        AgentRole.RESEARCH,
        AgentRole.ANALYSIS,
        AgentRole.REVIEWER,
    ),
    "mvp": (
        AgentRole.RESEARCH,
        AgentRole.CODE,
        AgentRole.ANALYSIS,
        AgentRole.VISUALIZATION,
        AgentRole.PAPER,
        AgentRole.REVIEWER,
    ),
    "scientist": tuple(AgentRole),
}


@dataclass(frozen=True)
class RuntimeReport:
    run_id: str
    profile: str
    legacy_mode: str | None
    provider_status: str
    provider_model: str
    agent_results: tuple[Mapping[str, Any], ...]
    report_path: str
    manifest_path: str
    created_at: str

    @property
    def completed_roles(self) -> tuple[str, ...]:
        return tuple(
            str(result["role"])
            for result in self.agent_results
            if result["status"] == AgentResultStatus.COMPLETED.value
        )

    @property
    def blocked_roles(self) -> tuple[str, ...]:
        return tuple(
            str(result["role"])
            for result in self.agent_results
            if result["status"] == AgentResultStatus.BLOCKED.value
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["completed_roles"] = list(self.completed_roles)
        payload["blocked_roles"] = list(self.blocked_roles)
        return redact_structure(payload)


class ResearchOSRuntime:
    """Concrete, evidence-producing runtime shared by every public entrypoint."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        profile: ExecutionProfile | str,
        memory: ScientificMemory,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.profile = ExecutionProfile(profile)
        self.policy = ExecutionPolicy(self.profile)
        self.memory = memory
        self.trace_store = PersistentTraceStore(self.workspace)
        self.artifacts = ArtifactStore(
            self.workspace,
            allowed_roots=(".paperforge/runtime",),
            allowed_suffixes=(".json",),
            allowed_kinds=("manifest", "report"),
            memory=memory,
        )
        self.registry = AgentRegistry(
            (
                ResearchAgent(self._handle_research),
                ExperimentAgent(self._handle_experiment),
                CodeAgent(self._handle_code),
                ComputeAgent(self._handle_compute),
                AnalysisAgent(self._handle_analysis),
                VisualizationAgent(self._handle_visualization),
                PaperAgent(self._handle_paper),
                ReviewerAgent(self._handle_reviewer),
                ReleaseAgent(self._handle_release),
            ),
            trace_store=self.trace_store,
        )

    def _workspace_files(self, suffixes: Iterable[str]) -> tuple[Path, ...]:
        allowed = {suffix.lower() for suffix in suffixes}
        files: list[Path] = []
        for path in self.workspace.rglob("*"):
            relative = path.relative_to(self.workspace)
            if any(part in _SKIPPED_ROOTS for part in relative.parts):
                continue
            if path.is_symlink() or not path.is_file():
                continue
            if path.suffix.lower() in allowed:
                files.append(path)
        return tuple(sorted(files))

    def _inventory(self, suffixes: Iterable[str]) -> dict[str, Any]:
        files = self._workspace_files(suffixes)
        entries = [
            {
                "path": path.relative_to(self.workspace).as_posix(),
                "sha256": sha256_file(path),
                "size_bytes": path.stat().st_size,
            }
            for path in files
        ]
        canonical = json.dumps(
            entries,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return {
            "file_count": len(entries),
            "inventory_sha256": hashlib.sha256(canonical).hexdigest(),
            "files": entries,
        }

    def _handle_research(
        self,
        request: AgentRequest,
        _trace_payloads: tuple[Any, ...],
    ) -> AgentResult:
        inventory = self._inventory((".bib", ".csv", ".json", ".md", ".pdf", ".tex"))
        return AgentResult.completed(
            request,
            {
                "operation": "evidence_inventory",
                **inventory,
            },
        )

    def _handle_experiment(
        self,
        request: AgentRequest,
        _trace_payloads: tuple[Any, ...],
    ) -> AgentResult:
        proposal_id = str(request.payload.get("proposal_id") or "").strip()
        if not proposal_id:
            return AgentResult(
                request_id=request.request_id,
                role=request.role,
                status=AgentResultStatus.BLOCKED,
                output={"reason": "proposal_id_required"},
            )
        manager = ExperimentManager(
            self.workspace,
            profile=self.profile,
            memory=self.memory,
        )
        proposal = manager.get_proposal(proposal_id)
        next_stage = manager.next_stage(proposal_id)
        return AgentResult.completed(
            request,
            {
                "operation": "experiment_state",
                "proposal_id": proposal_id,
                "status": proposal.status.value,
                "next_stage": next_stage.value if next_stage is not None else None,
            },
        )

    def _handle_code(
        self,
        request: AgentRequest,
        _trace_payloads: tuple[Any, ...],
    ) -> AgentResult:
        return AgentResult.completed(
            request,
            {
                "operation": "source_inventory",
                **self._inventory((".py", ".toml", ".yaml", ".yml")),
            },
        )

    def _handle_compute(
        self,
        request: AgentRequest,
        _trace_payloads: tuple[Any, ...],
    ) -> AgentResult:
        if not request.payload.get("job_spec"):
            return AgentResult(
                request_id=request.request_id,
                role=request.role,
                status=AgentResultStatus.BLOCKED,
                output={"reason": "approved_job_spec_required"},
            )
        return AgentResult.completed(
            request,
            {
                "operation": "compute_request_validated",
                "execution": "delegated_to_compute_backend",
            },
        )

    def _handle_analysis(
        self,
        request: AgentRequest,
        _trace_payloads: tuple[Any, ...],
    ) -> AgentResult:
        return AgentResult.completed(
            request,
            {
                "operation": "claim_gate_snapshot",
                "claim_gate": self.memory.claim_gate(final_publication=False),
            },
        )

    def _handle_visualization(
        self,
        request: AgentRequest,
        _trace_payloads: tuple[Any, ...],
    ) -> AgentResult:
        source_manifest = request.payload.get("visualization_manifest")
        if not source_manifest:
            return AgentResult(
                request_id=request.request_id,
                role=request.role,
                status=AgentResultStatus.BLOCKED,
                output={"reason": "visualization_manifest_required"},
            )
        return AgentResult.completed(
            request,
            {
                "operation": "visualization_manifest_validated",
                "source_manifest": str(source_manifest),
            },
        )

    def _handle_paper(
        self,
        request: AgentRequest,
        _trace_payloads: tuple[Any, ...],
    ) -> AgentResult:
        inventory = self._inventory((".bib", ".tex"))
        return AgentResult.completed(
            request,
            {
                "operation": "paper_inventory",
                "single_bibliography": sum(
                    entry["path"].endswith("references.bib")
                    for entry in inventory["files"]
                )
                <= 1,
                **inventory,
            },
        )

    def _handle_reviewer(
        self,
        request: AgentRequest,
        _trace_payloads: tuple[Any, ...],
    ) -> AgentResult:
        gate = self.memory.claim_gate(final_publication=True)
        status = (
            AgentResultStatus.COMPLETED
            if gate["passed"]
            else AgentResultStatus.BLOCKED
        )
        return AgentResult(
            request_id=request.request_id,
            role=request.role,
            status=status,
            output={
                "operation": "evidence_review",
                "claim_gate": gate,
            },
        )

    def _handle_release(
        self,
        request: AgentRequest,
        _trace_payloads: tuple[Any, ...],
    ) -> AgentResult:
        gate = self.memory.claim_gate(final_publication=True)
        if not gate["passed"]:
            return AgentResult(
                request_id=request.request_id,
                role=request.role,
                status=AgentResultStatus.BLOCKED,
                output={"reason": "claim_gate_failed", "claim_gate": gate},
            )
        return AgentResult.completed(
            request,
            {
                "operation": "release_prerequisites",
                "claim_gate": gate,
            },
        )

    def _roles(self, legacy_mode: str | None) -> tuple[AgentRole, ...]:
        if legacy_mode is None:
            return _PROFILE_ROLES[self.profile]
        try:
            requested = _LEGACY_ROLES[legacy_mode]
        except KeyError as exc:
            raise ValueError(f"unknown compatibility mode: {legacy_mode}") from exc
        allowed = set(_PROFILE_ROLES[self.profile])
        return tuple(role for role in requested if role in allowed)

    def _provider_status(self) -> tuple[str, str]:
        registry = ProviderRegistry()
        config = registry.resolve("bailu-turing", stage="writeup")
        credential = registry.credential(config)
        return ("CONFIGURED" if credential else "AUTH_BLOCKED", config.model)

    def execute(
        self,
        *,
        run_id: str,
        legacy_mode: str | None,
        inputs: Mapping[str, Any] | None = None,
    ) -> RuntimeReport:
        payload = dict(inputs or {})
        results: list[dict[str, Any]] = []
        for role in self._roles(legacy_mode):
            try:
                self.policy.require(_ROLE_ACTION[role])
            except PolicyViolation as exc:
                result = AgentResult(
                    request_id=f"{run_id}:{role.value}",
                    role=role,
                    status=AgentResultStatus.BLOCKED,
                    output={"reason": str(exc)},
                )
            else:
                result = self.registry.dispatch(
                    AgentRequest(
                        request_id=f"{run_id}:{role.value}",
                        role=role,
                        task=f"Execute {role.value} stage for workflow {run_id}.",
                        payload=payload,
                    )
                )
            results.append(result.to_dict())

        provider_status, provider_model = self._provider_status()
        report_path = f".paperforge/runtime/{run_id}.json"
        manifest_path = f".paperforge/runtime/{run_id}-manifest.json"
        report_payload = {
            "schema": "paperforge.runtime-report/v1",
            "run_id": run_id,
            "profile": self.profile.value,
            "legacy_mode": legacy_mode,
            "provider_status": provider_status,
            "provider_model": provider_model,
            "agent_results": results,
            "created_at": utc_now(),
        }
        self.artifacts.write_json(
            report_path,
            report_payload,
            kind="report",
            metadata={"workflow_id": run_id},
        )
        self.artifacts.write_manifest(
            manifest_path,
            kind="manifest",
            metadata={"workflow_id": run_id},
        )
        return RuntimeReport(
            run_id=run_id,
            profile=self.profile.value,
            legacy_mode=legacy_mode,
            provider_status=provider_status,
            provider_model=provider_model,
            agent_results=tuple(results),
            report_path=report_path,
            manifest_path=manifest_path,
            created_at=str(report_payload["created_at"]),
        )


__all__ = ["ResearchOSRuntime", "RuntimeReport"]
