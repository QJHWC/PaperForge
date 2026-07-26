from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Iterable, Mapping
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from engine.secret_redaction import contains_secret, redact_structure

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
from .compute import (
    ArtifactDirection,
    CloudSSHConfig,
    DockerConfig,
    JobSpec,
    JobStatus,
    KubernetesConfig,
    SlurmConfig,
    SSHConfig,
    UnknownJobError,
    build_compute_binding,
    create_backend,
    verify_compute_binding,
)
from .compute._artifacts import copy_local_artifacts
from .experiments import ExperimentManager, ProvenanceKind
from .models import (
    ExecutionProfile,
    ExperimentStage,
    ExperimentStatus,
    utc_now,
)
from .path_safety import safe_mkdir
from .plugins import builtin_registry
from .policy import Action, ExecutionPolicy, PolicyViolation
from .provider import (
    ProviderAuthenticationError,
    ProviderRegistry,
    chat_completion_text,
)
from .scientific_memory import ScientificMemory
from .visualization import VisualizationExporter
from .writing import WritingEngine, WritingError

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

    @property
    def failed_roles(self) -> tuple[str, ...]:
        return tuple(
            str(result["role"])
            for result in self.agent_results
            if result["status"] == AgentResultStatus.FAILED.value
        )

    @property
    def skipped_roles(self) -> tuple[str, ...]:
        return tuple(
            str(result["role"])
            for result in self.agent_results
            if result["status"] == AgentResultStatus.SKIPPED.value
        )

    @property
    def auth_blocked(self) -> bool:
        return any(
            result["status"] == AgentResultStatus.BLOCKED.value
            and isinstance(result.get("output"), Mapping)
            and result["output"].get("reason") == "AUTH_BLOCKED"
            for result in self.agent_results
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["completed_roles"] = list(self.completed_roles)
        payload["blocked_roles"] = list(self.blocked_roles)
        payload["failed_roles"] = list(self.failed_roles)
        payload["skipped_roles"] = list(self.skipped_roles)
        payload["auth_blocked"] = self.auth_blocked
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
        raw_spec = request.payload.get("job_spec")
        if not isinstance(raw_spec, Mapping):
            return AgentResult(
                request_id=request.request_id,
                role=request.role,
                status=AgentResultStatus.BLOCKED,
                output={"reason": "approved_job_spec_required"},
            )
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
        approval = manager.require_approval(proposal_id, "full_experiment")
        backend_name = str(
            request.payload.get("compute_backend") or "local"
        ).strip().lower().replace("_", "-")
        normalized_backend = backend_name
        raw_config = request.payload.get("compute_config") or {}
        if not isinstance(raw_config, Mapping):
            raise TypeError("compute_config must be a mapping")
        persisted_binding = proposal.metadata.get("compute_binding")
        if isinstance(persisted_binding, Mapping):
            try:
                supplied_spec, supplied_binding = build_compute_binding(
                    self.workspace,
                    job_spec=raw_spec,
                    compute_backend=normalized_backend,
                    compute_config=raw_config,
                )
            except (TypeError, ValueError, OSError) as exc:
                return AgentResult(
                    request_id=request.request_id,
                    role=request.role,
                    status=AgentResultStatus.BLOCKED,
                    output={"reason": f"compute_binding_invalid: {exc}"},
                )
            if supplied_binding.get("binding_sha256") != persisted_binding.get(
                "binding_sha256"
            ):
                return AgentResult(
                    request_id=request.request_id,
                    role=request.role,
                    status=AgentResultStatus.BLOCKED,
                    output={"reason": "job_manifest_does_not_match_proposal"},
                )
            verified, verification_detail = verify_compute_binding(
                self.workspace,
                persisted_binding,
            )
            if not verified:
                return AgentResult(
                    request_id=request.request_id,
                    role=request.role,
                    status=AgentResultStatus.BLOCKED,
                    output={
                        "reason": "compute_binding_changed",
                        "detail": verification_detail,
                    },
                )
            spec = supplied_spec
            config = dict(persisted_binding.get("compute_config") or {})
        else:
            spec = JobSpec.from_dict(raw_spec)
            config = dict(raw_config)
            if spec.execute:
                return AgentResult(
                    request_id=request.request_id,
                    role=request.role,
                    status=AgentResultStatus.BLOCKED,
                    output={"reason": "immutable_compute_binding_required"},
                )
        if tuple(spec.command) != tuple(proposal.command):
            return AgentResult(
                request_id=request.request_id,
                role=request.role,
                status=AgentResultStatus.BLOCKED,
                output={"reason": "job_command_does_not_match_proposal"},
            )
        if spec.execute:
            proposal_runs = manager.list_runs(proposal_id)
            verified_runs = [
                run
                for run in proposal_runs
                if run.status is ExperimentStatus.PASSED
                and run.metadata.get("execution_verified") is True
            ]
            verified_stages = {run.stage for run in verified_runs}
            required_stages = {
                ExperimentStage.STATIC_CHECK,
                ExperimentStage.MINI_EXPERIMENT,
            }
            missing_stages = sorted(
                stage.value for stage in required_stages - verified_stages
            )
            if missing_stages:
                return AgentResult(
                    request_id=request.request_id,
                    role=request.role,
                    status=AgentResultStatus.BLOCKED,
                    output={
                        "reason": "experiment_stage_gate_required",
                        "missing_stages": missing_stages,
                    },
                )
            completed_full_runs = [
                run
                for run in verified_runs
                if run.stage is ExperimentStage.FULL_EXPERIMENT
            ]
            if completed_full_runs:
                run = completed_full_runs[-1]
                return AgentResult.completed(
                    request,
                    {
                        "operation": "compute_completed",
                        "backend": normalized_backend,
                        "job": {
                            "executed": True,
                            "reused": True,
                            "status": run.status.value,
                            "run_id": run.run_id,
                            "eligible_for_claims": run.eligible_for_claims,
                        },
                    },
                )
            if manager.next_stage(proposal_id) is not ExperimentStage.FULL_EXPERIMENT:
                return AgentResult(
                    request_id=request.request_id,
                    role=request.role,
                    status=AgentResultStatus.BLOCKED,
                    output={"reason": "experiment_stage_order_invalid"},
                )
        scope = approval.get("scope")
        if not isinstance(scope, Mapping):
            scope = {}
        compute_config_sha256 = hashlib.sha256(
            json.dumps(
                config,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        binding_sha256 = (
            persisted_binding.get("binding_sha256")
            if isinstance(persisted_binding, Mapping)
            else None
        )
        if spec.execute and (
            not binding_sha256
            or scope.get("compute_binding_sha256") != binding_sha256
            or scope.get("job_fingerprint") != spec.fingerprint
            or str(scope.get("compute_backend", "")).strip().lower().replace(
                "_", "-"
            )
            != normalized_backend
            or str(scope.get("worktree", "")) != str(spec.workdir)
            or scope.get("compute_config_sha256")
            != compute_config_sha256
        ):
            return AgentResult(
                request_id=request.request_id,
                role=request.role,
                status=AgentResultStatus.BLOCKED,
                output={"reason": "compute_scope_does_not_match_job"},
            )
        if (
            spec.execute
            and normalized_backend == "local"
            and isinstance(persisted_binding, Mapping)
        ):
            try:
                run = manager.execute_stage(
                    proposal_id,
                    ExperimentStage.FULL_EXPERIMENT,
                    provenance_kind=ProvenanceKind.MEASURED,
                    checkpoint_paths=tuple(
                        str(path)
                        for path in spec.metadata.get("checkpoint_paths", ())
                    ),
                    metrics_paths=tuple(
                        str(path)
                        for path in spec.metadata.get("metrics_paths", ())
                    ),
                )
            except (OSError, RuntimeError, ValueError) as exc:
                return AgentResult(
                    request_id=request.request_id,
                    role=request.role,
                    status=AgentResultStatus.BLOCKED,
                    output={"reason": f"full_experiment_failed: {exc}"},
                )
            return AgentResult.completed(
                request,
                {
                    "operation": "compute_completed",
                    "backend": "local",
                    "job": {
                        "executed": True,
                        "status": run.status.value,
                        "run_id": run.run_id,
                        "eligible_for_claims": run.eligible_for_claims,
                    },
                },
            )
        common = {
            "policy": self.policy,
            "state_dir": self.workspace / ".paperforge" / "compute",
        }
        execution_worktree: str | None = None
        if spec.execute and isinstance(persisted_binding, Mapping):
            execution_worktree = persisted_binding.get("execution_worktree")
            if not isinstance(execution_worktree, str) or not execution_worktree:
                return AgentResult(
                    request_id=request.request_id,
                    role=request.role,
                    status=AgentResultStatus.BLOCKED,
                    output={
                        "reason": "compute_execution_snapshot_required"
                    },
                )
        if spec.execute and normalized_backend == "docker":
            assert execution_worktree is not None
            config["workspace_mount"] = execution_worktree
        if normalized_backend == "local":
            backend = create_backend(backend_name, **common)
        elif normalized_backend == "docker":
            backend = create_backend(
                backend_name,
                DockerConfig(**config),
                **common,
            )
        elif normalized_backend == "slurm":
            backend = create_backend(
                backend_name,
                SlurmConfig(**config),
                **common,
            )
        elif normalized_backend == "kubernetes":
            backend = create_backend(
                backend_name,
                KubernetesConfig(**config),
                **common,
            )
        elif normalized_backend == "ssh":
            backend = create_backend(
                backend_name,
                SSHConfig(**config),
                **common,
            )
        elif normalized_backend == "cloud-ssh":
            ssh_payload = config.pop("ssh", None)
            if not isinstance(ssh_payload, Mapping):
                raise TypeError("cloud-ssh compute_config requires ssh mapping")
            backend = create_backend(
                backend_name,
                CloudSSHConfig(
                    ssh=SSHConfig(**dict(ssh_payload)),
                    **config,
                ),
                **common,
            )
        else:
            raise ValueError(f"unknown compute backend: {backend_name}")
        if spec.execute and isinstance(persisted_binding, Mapping):
            verified, verification_detail = verify_compute_binding(
                self.workspace,
                persisted_binding,
            )
            if not verified:
                return AgentResult(
                    request_id=request.request_id,
                    role=request.role,
                    status=AgentResultStatus.BLOCKED,
                    output={
                        "reason": "compute_binding_changed_before_submit",
                        "detail": verification_detail,
                    },
                )
        if not spec.execute:
            result = backend.submit(spec)
            return AgentResult.completed(
                request,
                {
                    "operation": "compute_planned",
                    "backend": backend.name,
                    "job": result.to_dict(),
                },
            )
        if spec.job_id is None or execution_worktree is None:
            return AgentResult(
                request_id=request.request_id,
                role=request.role,
                status=AgentResultStatus.BLOCKED,
                output={"reason": "stable_compute_job_id_required"},
            )
        lifecycle_id = (
            f"{normalized_backend}:{spec.job_id}:{binding_sha256}"
        )
        try:
            budget_event_id, _ = manager.reserve_backend_stage(
                proposal_id,
                ExperimentStage.FULL_EXPERIMENT,
                lifecycle_id=lifecycle_id,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            return AgentResult(
                request_id=request.request_id,
                role=request.role,
                status=AgentResultStatus.BLOCKED,
                output={
                    "reason": f"compute_budget_reservation_failed: {exc}"
                },
            )
        execution_spec = spec
        if normalized_backend == "slurm":
            payload = spec.to_dict()
            payload["workdir"] = execution_worktree
            execution_spec = JobSpec.from_dict(payload)
        try:
            result = backend.status(spec.job_id, execute=True)
        except UnknownJobError:
            if normalized_backend in {
                "ssh",
                "cloud-ssh",
                "kubernetes",
            }:
                stage_source = getattr(backend, "stage_source", None)
                if not callable(stage_source):
                    manager.release_backend_stage(
                        proposal_id,
                        lifecycle_id=lifecycle_id,
                        budget_event_id=budget_event_id,
                        reason="source_staging_not_supported",
                    )
                    return AgentResult(
                        request_id=request.request_id,
                        role=request.role,
                        status=AgentResultStatus.BLOCKED,
                        output={
                            "reason": "remote_source_staging_not_supported"
                        },
                    )
                staged = stage_source(
                    execution_spec,
                    execution_worktree,
                    execute=True,
                )
                if staged.status is not JobStatus.SUCCEEDED:
                    manager.release_backend_stage(
                        proposal_id,
                        lifecycle_id=lifecycle_id,
                        budget_event_id=budget_event_id,
                        reason="source_staging_failed",
                    )
                    return AgentResult.failed(
                        request,
                        staged.message or "remote source staging failed",
                    )
            try:
                result = backend.submit(execution_spec)
            except (OSError, RuntimeError, ValueError) as exc:
                manager.release_backend_stage(
                    proposal_id,
                    lifecycle_id=lifecycle_id,
                    budget_event_id=budget_event_id,
                    reason="backend_submit_failed",
                )
                return AgentResult.failed(
                    request,
                    f"compute submission failed: {exc}",
                )
        if result.status in {
            JobStatus.SUBMITTED,
            JobStatus.QUEUED,
            JobStatus.RUNNING,
            JobStatus.SUSPENDED,
        }:
            return AgentResult(
                request_id=request.request_id,
                role=request.role,
                status=AgentResultStatus.BLOCKED,
                output={
                    "reason": "compute_job_in_progress",
                    "backend": backend.name,
                    "job": result.to_dict(),
                },
            )
        if result.status is JobStatus.UNKNOWN:
            return AgentResult(
                request_id=request.request_id,
                role=request.role,
                status=AgentResultStatus.BLOCKED,
                output={
                    "reason": "compute_job_state_unknown",
                    "backend": backend.name,
                    "job": result.to_dict(),
                },
            )
        raw_actual_cost = result.metadata.get("actual_cost")
        try:
            charged_cost = manager.charge_backend_stage(
                proposal_id,
                ExperimentStage.FULL_EXPERIMENT,
                lifecycle_id=lifecycle_id,
                budget_event_id=budget_event_id,
                actual_cost=(
                    float(raw_actual_cost)
                    if isinstance(raw_actual_cost, int | float)
                    and not isinstance(raw_actual_cost, bool)
                    else None
                ),
            )
        except (OSError, RuntimeError, ValueError) as exc:
            return AgentResult(
                request_id=request.request_id,
                role=request.role,
                status=AgentResultStatus.BLOCKED,
                output={"reason": f"compute_cost_finalization_failed: {exc}"},
            )
        logs = backend.logs(spec.job_id, execute=True)
        return_code = result.return_code
        if return_code is None:
            return_code = (
                0 if result.status is JobStatus.SUCCEEDED else 1
            )
        if result.status is not JobStatus.SUCCEEDED:
            with suppress(OSError, RuntimeError, ValueError):
                manager.finalize_backend_stage(
                    proposal_id,
                    ExperimentStage.FULL_EXPERIMENT,
                    lifecycle_id=lifecycle_id,
                    budget_event_id=budget_event_id,
                    returncode=return_code,
                    stdout=logs.stdout,
                    stderr=logs.stderr,
                    metadata={
                        "backend": backend.name,
                        "job": result.to_dict(),
                    },
                )
            return AgentResult.failed(
                request,
                result.message or "compute execution failed",
            )
        try:
            staging_parent = safe_mkdir(
                self.workspace / ".paperforge" / "artifact-staging",
                anchor=self.workspace,
            )
            with tempfile.TemporaryDirectory(
                prefix="paperforge-remote-artifacts-",
                dir=staging_parent,
            ) as temporary:
                staging = Path(temporary)
                synced = backend.sync_artifacts(
                    spec.job_id,
                    staging,
                    direction=ArtifactDirection.DOWNLOAD,
                    patterns=tuple(str(path) for path in spec.outputs),
                    execute=True,
                )
                if synced.status is not JobStatus.SUCCEEDED:
                    detail = synced.stderr.strip()
                    message = (
                        f"{synced.message}: {detail}"
                        if synced.message and detail
                        else synced.message
                        or detail
                        or "compute artifact sync failed"
                    )
                    raise RuntimeError(message)
                artifacts = copy_local_artifacts(
                    source_root=staging,
                    destination_root=self.workspace,
                    patterns=tuple(str(path) for path in spec.outputs),
                )
            run = manager.finalize_backend_stage(
                proposal_id,
                ExperimentStage.FULL_EXPERIMENT,
                lifecycle_id=lifecycle_id,
                budget_event_id=budget_event_id,
                returncode=return_code,
                stdout=logs.stdout,
                stderr=logs.stderr,
                checkpoint_paths=tuple(
                    str(path)
                    for path in spec.metadata.get("checkpoint_paths", ())
                ),
                metrics_paths=tuple(
                    str(path)
                    for path in spec.metadata.get("metrics_paths", ())
                ),
                actual_cost=charged_cost,
                metadata={
                    "backend": backend.name,
                    "job": result.to_dict(),
                    "artifacts": [
                        artifact.to_dict() for artifact in artifacts
                    ],
                },
            )
        except (OSError, RuntimeError, ValueError) as exc:
            return AgentResult(
                request_id=request.request_id,
                role=request.role,
                status=AgentResultStatus.BLOCKED,
                output={
                    "reason": f"compute_finalization_failed: {exc}"
                },
            )
        return AgentResult.completed(
            request,
            {
                "operation": "compute_completed",
                "backend": backend.name,
                "job": {
                    **result.to_dict(),
                    "run_id": run.run_id,
                    "eligible_for_claims": run.eligible_for_claims,
                },
            },
        )

    def _handle_analysis(
        self,
        request: AgentRequest,
        _trace_payloads: tuple[Any, ...],
    ) -> AgentResult:
        plugin_name = request.payload.get("domain_plugin")
        observed_rows = request.payload.get("observed_rows")
        if plugin_name is None and observed_rows is None:
            return AgentResult.completed(
                request,
                {
                    "operation": "claim_gate_snapshot",
                    "claim_gate": self.memory.claim_gate(
                        final_publication=False
                    ),
                },
            )
        if not isinstance(plugin_name, str) or not isinstance(
            observed_rows,
            list | tuple,
        ):
            raise TypeError(
                "domain_plugin and observed_rows are required together"
            )
        if any(not isinstance(row, Mapping) for row in observed_rows):
            raise TypeError("every observed row must be a mapping")
        result = builtin_registry.run(plugin_name, observed_rows)
        evidence_ids = []
        for record in result.evidence:
            evidence_ids.append(
                self.memory.add_evidence(
                    **record.to_scientific_memory_kwargs()
                )
            )
        return AgentResult.completed(
            request,
            {
                "operation": "domain_analysis",
                "plugin_result": result.to_dict(),
                "evidence_ids": evidence_ids,
            },
        )

    def _handle_visualization(
        self,
        request: AgentRequest,
        _trace_payloads: tuple[Any, ...],
    ) -> AgentResult:
        plugin_name = request.payload.get("domain_plugin")
        observed_rows = request.payload.get("observed_rows")
        if plugin_name is None and observed_rows is None:
            return AgentResult.skipped(
                request,
                {
                    "operation": "visualization_skipped",
                    "reason": "no_domain_visualization_requested",
                },
            )
        if not isinstance(plugin_name, str) or not isinstance(
            observed_rows, list | tuple
        ):
            return AgentResult(
                request_id=request.request_id,
                role=request.role,
                status=AgentResultStatus.BLOCKED,
                output={
                    "reason": (
                        "domain_plugin_and_observed_rows_required"
                    )
                },
            )
        if any(not isinstance(row, Mapping) for row in observed_rows):
            raise TypeError("every observed row must be a mapping")
        result = builtin_registry.run(plugin_name, observed_rows)
        name = (
            f"{result.plugin}-"
            f"{hashlib.sha256(request.request_id.encode('utf-8')).hexdigest()[:12]}"
        )
        records = VisualizationExporter(
            self.workspace,
            memory=self.memory,
        ).export(
            result.visualizations[0],
            name=name,
            workflow_id=request.request_id.rsplit(":", 1)[0],
            source_manifest={
                "plugin": result.plugin,
                "validated_rows": result.validated_rows,
                "metrics": dict(result.metrics),
                "evidence": [
                    record.to_dict() for record in result.evidence
                ],
            },
        )
        return AgentResult.completed(
            request,
            {
                "operation": "visualization_exported",
                "artifacts": [record.to_dict() for record in records],
            },
            artifacts=tuple(record.path for record in records),
        )

    def _handle_paper(
        self,
        request: AgentRequest,
        _trace_payloads: tuple[Any, ...],
    ) -> AgentResult:
        inventory = self._inventory((".bib", ".tex"))
        writing_fields = {
            "abstract",
            "bibliography_path",
            "document_path",
            "instructions",
            "main_tex",
            "main_tex_path",
            "outline",
            "title",
            "topic",
        }
        writing_requested = any(
            key in request.payload and request.payload.get(key) not in (None, "", [], ())
            for key in writing_fields
        )
        if not writing_requested:
            if self.profile is ExecutionProfile.WRITING_ONLY:
                return AgentResult(
                    request_id=request.request_id,
                    role=request.role,
                    status=AgentResultStatus.BLOCKED,
                    output={
                        "reason": "writing_request_required",
                        "operation": "paper_inventory",
                        **inventory,
                    },
                )
            return AgentResult.skipped(
                request,
                {
                    "operation": "paper_skipped",
                    "reason": "no_writing_requested",
                    **inventory,
                },
            )
        try:
            result = WritingEngine(
                self.workspace,
                memory=self.memory,
                request_text=chat_completion_text,
            ).write(
                run_id=request.request_id.rsplit(":", 1)[0],
                payload=request.payload,
                model="bailu-turing",
            )
        except ProviderAuthenticationError:
            return AgentResult(
                request_id=request.request_id,
                role=request.role,
                status=AgentResultStatus.BLOCKED,
                output={
                    "reason": "AUTH_BLOCKED",
                    "operation": "paper_write",
                },
            )
        except WritingError as exc:
            return AgentResult(
                request_id=request.request_id,
                role=request.role,
                status=AgentResultStatus.BLOCKED,
                output={
                    "reason": "writing_safety_gate_failed",
                    "detail": str(exc),
                },
            )
        return AgentResult.completed(
            request,
            {
                "operation": "paper_written",
                "draft": result.to_dict(),
                "single_bibliography": sum(
                    entry["path"].endswith("references.bib")
                    for entry in inventory["files"]
                )
                <= 1,
                **inventory,
            },
            artifacts=(result.artifact.path,),
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
        if contains_secret(payload):
            raise ValueError(
                "runtime inputs must not contain credentials or secret-like values"
            )
        results: list[dict[str, Any]] = []
        halted_by: str | None = None
        for role in self._roles(legacy_mode):
            if halted_by is not None:
                result = AgentResult(
                    request_id=f"{run_id}:{role.value}",
                    role=role,
                    status=AgentResultStatus.BLOCKED,
                    output={
                        "reason": "upstream_dependency_failed",
                        "upstream_role": halted_by,
                    },
                )
            else:
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
                    try:
                        result = self.registry.dispatch(
                            AgentRequest(
                                request_id=f"{run_id}:{role.value}",
                                role=role,
                                task=(
                                    f"Execute {role.value} stage for workflow "
                                    f"{run_id}."
                                ),
                                payload=payload,
                            )
                        )
                    except Exception as exc:
                        result = AgentResult(
                            request_id=f"{run_id}:{role.value}",
                            role=role,
                            status=AgentResultStatus.FAILED,
                            error=type(exc).__name__,
                            output={"reason": str(exc)},
                        )
            if result.status in {
                AgentResultStatus.BLOCKED,
                AgentResultStatus.FAILED,
            }:
                halted_by = role.value
            results.append(redact_structure(result.to_dict()))

        provider_status, provider_model = self._provider_status()
        if any(
            result["status"] == AgentResultStatus.BLOCKED.value
            and isinstance(result.get("output"), Mapping)
            and result["output"].get("reason") == "AUTH_BLOCKED"
            for result in results
        ):
            provider_status = "AUTH_BLOCKED"
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
