from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from engine.secret_redaction import contains_secret

from .compute.binding import build_compute_binding, verify_compute_binding
from .compute.contracts import ArtifactDirection, JobSpec, JobStatus
from .compute.local import LocalBackend
from .models import (
    ExecutionProfile,
    ExperimentStage,
    ExperimentStatus,
    utc_now,
)
from .policy import Action, ExecutionPolicy
from .scientific_memory import ScientificMemory, _stable_id


class ExperimentError(RuntimeError):
    """Base class for experiment orchestration failures."""


class ExperimentTransitionError(ExperimentError):
    """Raised when a proposal attempts to skip or repeat a stage."""


class ExperimentIntegrityError(ExperimentError):
    """Raised when a provenance input cannot be hashed safely."""


class ProvenanceViolation(ExperimentError, ValueError):
    """Raised when provenance metadata is used as publishable evidence."""


class ProvenanceKind(str, Enum):
    MEASURED = "measured"
    IMPORTED = "imported"
    SIMULATED = "simulated"
    INFERRED = "inferred"


_STAGE_SEQUENCE = (
    ExperimentStage.STATIC_CHECK,
    ExperimentStage.MINI_EXPERIMENT,
    ExperimentStage.FULL_EXPERIMENT,
)
_STAGE_ACTIONS = {
    ExperimentStage.STATIC_CHECK: Action.EXPERIMENT_STATIC,
    ExperimentStage.MINI_EXPERIMENT: Action.EXPERIMENT_MINI,
    ExperimentStage.FULL_EXPERIMENT: Action.EXPERIMENT_FULL,
}
_STAGE_ALIASES = {
    "static": ExperimentStage.STATIC_CHECK,
    "static_check": ExperimentStage.STATIC_CHECK,
    "mini": ExperimentStage.MINI_EXPERIMENT,
    "mini_experiment": ExperimentStage.MINI_EXPERIMENT,
    "full": ExperimentStage.FULL_EXPERIMENT,
    "full_experiment": ExperimentStage.FULL_EXPERIMENT,
}
_SIMULATED_CLAIM_KEYS = {
    "claim",
    "claims",
    "conclusion",
    "conclusions",
    "performance_claim",
    "performance_conclusion",
    "publication_claim",
    "publishable_claim",
}
_FINAL_RUN_STATUSES = {
    ExperimentStatus.PASSED,
    ExperimentStatus.FAILED,
    ExperimentStatus.CANCELLED,
}
_EXECUTION_VERIFIED = object()


def _normalize_stage(value: ExperimentStage | str) -> ExperimentStage:
    if isinstance(value, ExperimentStage):
        return value
    raw = str(value).strip()
    try:
        return ExperimentStage(raw)
    except ValueError:
        normalized = raw.lower().replace("-", "_")
        try:
            return _STAGE_ALIASES[normalized]
        except KeyError as exc:
            raise ExperimentTransitionError(f"unknown experiment stage: {value!r}") from exc


def _normalize_paths(values: Iterable[str | Path] | str | Path | None) -> tuple[str, ...]:
    if values is None:
        return ()
    raw_values: Iterable[str | Path]
    raw_values = (values,) if isinstance(values, str | Path) else values
    normalized: list[str] = []
    for value in raw_values:
        raw = os.fspath(value)
        if contains_secret(raw):
            raise ExperimentIntegrityError(
                "experiment provenance path must not contain credentials"
            )
        path = Path(raw)
        if (
            not raw
            or "\x00" in raw
            or "\\" in raw
            or path.is_absolute()
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ExperimentIntegrityError(
                f"experiment provenance path must be workspace-relative: {raw!r}"
            )
        rendered = path.as_posix()
        if rendered not in normalized:
            normalized.append(rendered)
    return tuple(normalized)


def _normalize_command(command: Sequence[str] | None) -> tuple[str, ...]:
    if command is None:
        return ()
    if isinstance(command, str | bytes):
        raise ValueError("experiment command must be an argument sequence, not a shell string")
    normalized = tuple(str(part) for part in command)
    if any(not part or "\x00" in part for part in normalized):
        raise ValueError("experiment command contains an empty or invalid argument")
    if contains_secret({"command": normalized}):
        raise ValueError("experiment command must not contain credentials")
    return normalized


def _json_mapping(value: Mapping[str, Any] | None) -> dict[str, Any]:
    normalized = dict(value or {})
    if contains_secret(normalized):
        raise ValueError("experiment metadata must not contain credentials")
    try:
        json.dumps(normalized, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ValueError("experiment metadata must be JSON serializable") from exc
    return normalized


@dataclass(frozen=True)
class ExperimentProposal:
    proposal_id: str
    title: str
    status: ExperimentStatus
    command: tuple[str, ...] = ()
    code_paths: tuple[str, ...] = ()
    config_paths: tuple[str, ...] = ()
    data_paths: tuple[str, ...] = ()
    output_paths: tuple[str, ...] = ()
    cost_limit: float | None = None
    risk_level: str | None = None
    created_at: str = ""
    approved_at: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def experiment_id(self) -> str:
        return self.proposal_id

    @property
    def id(self) -> str:
        return self.proposal_id

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        payload["command"] = list(self.command)
        payload["code_paths"] = list(self.code_paths)
        payload["config_paths"] = list(self.config_paths)
        payload["data_paths"] = list(self.data_paths)
        payload["output_paths"] = list(self.output_paths)
        payload["metadata"] = dict(self.metadata)
        return payload


@dataclass(frozen=True)
class ExperimentRunRecord:
    run_id: str
    proposal_id: str
    stage: ExperimentStage
    status: ExperimentStatus
    provenance_kind: ProvenanceKind
    eligible_for_claims: bool
    code_sha256: str | None = None
    config_sha256: str | None = None
    data_sha256: str | None = None
    checkpoint_sha256: str | None = None
    metrics_sha256: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def experiment_id(self) -> str:
        return self.proposal_id

    @property
    def id(self) -> str:
        return self.run_id

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["stage"] = self.stage.value
        payload["status"] = self.status.value
        payload["provenance_kind"] = self.provenance_kind.value
        payload["metadata"] = dict(self.metadata)
        return payload


# Concise alias used by callers that do not need to distinguish DB records.
ExperimentRun = ExperimentRunRecord


@dataclass(frozen=True)
class ExecutionOutcome:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    checkpoint_paths: tuple[str, ...] = ()
    metrics_paths: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


ExperimentExecutor = Callable[
    [tuple[str, ...], Path, ExperimentStage],
    ExecutionOutcome | subprocess.CompletedProcess[str] | Mapping[str, Any],
]


class ExperimentManager:
    """Approval-gated proposal -> static -> mini -> full state machine."""

    def __init__(
        self,
        workspace: str | Path,
        profile: ExecutionProfile | str = ExecutionProfile.FULL,
        *,
        memory: ScientificMemory | None = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.policy = ExecutionPolicy.from_value(profile)
        self.memory = memory or ScientificMemory(
            self.workspace / ".paperforge" / "paperforge.db",
            trusted_root=self.workspace,
        )

    @property
    def profile(self) -> ExecutionProfile:
        return self.policy.profile

    def _resolve_input(self, relative_path: str) -> Path:
        normalized = _normalize_paths((relative_path,))[0]
        lexical = self.workspace / normalized
        candidate = self.workspace
        for part in Path(normalized).parts:
            candidate = candidate / part
            if candidate.is_symlink():
                raise ExperimentIntegrityError(
                    f"provenance input cannot contain a symlink: {normalized}"
                )
            if not candidate.exists():
                break
        resolved = lexical.resolve(strict=False)
        if resolved == self.workspace or self.workspace not in resolved.parents:
            raise ExperimentIntegrityError(
                f"provenance input escapes workspace: {normalized}"
            )
        if not resolved.exists():
            raise ExperimentIntegrityError(f"provenance input is missing: {normalized}")
        return resolved

    @staticmethod
    def _hash_regular_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _hash_one(self, relative_path: str) -> str:
        path = self._resolve_input(relative_path)
        if path.is_file():
            return self._hash_regular_file(path)
        if not path.is_dir():
            raise ExperimentIntegrityError(
                f"provenance input must be a regular file or directory: {relative_path}"
            )
        entries: list[tuple[str, str]] = []
        for child in sorted(path.rglob("*")):
            if child.is_symlink():
                raise ExperimentIntegrityError(
                    f"provenance directory contains a symlink: {relative_path}"
                )
            if child.is_file():
                entries.append(
                    (
                        child.relative_to(path).as_posix(),
                        self._hash_regular_file(child),
                    )
                )
        if not entries:
            raise ExperimentIntegrityError(
                f"provenance directory contains no files: {relative_path}"
            )
        return hashlib.sha256(
            json.dumps(entries, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()

    def _hash_paths(
        self,
        paths: tuple[str, ...],
    ) -> tuple[str | None, list[dict[str, str]]]:
        entries = [
            {"path": path, "sha256": self._hash_one(path)}
            for path in paths
        ]
        if not entries:
            return None, []
        if len(entries) == 1:
            return entries[0]["sha256"], entries
        aggregate = hashlib.sha256(
            json.dumps(
                entries,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        return aggregate, entries

    def _output_hashes(
        self,
        paths: tuple[str, ...],
    ) -> dict[str, str | None]:
        hashes: dict[str, str | None] = {}
        for relative_path in paths:
            lexical = self.workspace / relative_path
            current = self.workspace
            for part in Path(relative_path).parts:
                current /= part
                if current.is_symlink():
                    raise ExperimentIntegrityError(
                        f"experiment output contains a symlink: {relative_path}"
                    )
                if not current.exists():
                    break
            resolved = lexical.resolve(strict=False)
            if resolved == self.workspace or self.workspace not in resolved.parents:
                raise ExperimentIntegrityError(
                    f"experiment output escapes workspace: {relative_path}"
                )
            hashes[relative_path] = (
                self._hash_one(relative_path) if resolved.exists() else None
            )
        return hashes

    def _validate_stage_bindings(
        self,
        stage_bindings: Mapping[str, Any],
        *,
        full_command: tuple[str, ...],
        require_complete: bool,
    ) -> dict[str, dict[str, Any]]:
        normalized: dict[str, dict[str, Any]] = {}
        allowed = {"static_check", "mini_experiment"}
        unknown = sorted(set(stage_bindings) - allowed)
        if unknown:
            raise ExperimentIntegrityError(
                "unsupported experiment stage bindings: " + ", ".join(unknown)
            )
        if require_complete and set(stage_bindings) != allowed:
            raise ExperimentIntegrityError(
                "executable experiments require static_check and mini_experiment bindings"
            )
        for stage_name, raw_binding in stage_bindings.items():
            if not isinstance(raw_binding, Mapping):
                raise ExperimentIntegrityError(
                    f"experiment stage {stage_name} binding is invalid"
                )
            binding = dict(raw_binding)
            verified, detail = verify_compute_binding(self.workspace, binding)
            if not verified:
                raise ExperimentIntegrityError(
                    f"experiment stage {stage_name} binding changed: {detail}"
                )
            raw_spec = binding.get("job_spec")
            if not isinstance(raw_spec, Mapping):
                raise ExperimentIntegrityError(
                    f"experiment stage {stage_name} has no job specification"
                )
            spec = JobSpec.from_dict(raw_spec)
            if binding.get("backend") != "local" or not spec.execute:
                raise ExperimentIntegrityError(
                    f"experiment stage {stage_name} must be an executable "
                    "local sandbox job"
                )
            if tuple(spec.command) == full_command:
                raise ExperimentIntegrityError(
                    f"experiment stage {stage_name} must use a distinct command"
                )
            self._estimated_cost(spec)
            if stage_name == "static_check" and (
                spec.resources.gpus != 0
                or (
                    spec.resources.timeout_seconds is not None
                    and spec.resources.timeout_seconds > 300
                )
            ):
                raise ExperimentIntegrityError(
                    "static_check is limited to zero GPUs and 300 seconds"
                )
            if stage_name == "mini_experiment" and (
                spec.resources.gpus > 1
                or (
                    spec.resources.timeout_seconds is not None
                    and spec.resources.timeout_seconds > 1800
                )
            ):
                raise ExperimentIntegrityError(
                    "mini_experiment is limited to one GPU and 1800 seconds"
                )
            normalized[stage_name] = binding
        return normalized

    def propose(
        self,
        *,
        title: str,
        command: Sequence[str] | None = None,
        code_paths: Iterable[str | Path] | str | Path = (),
        config_paths: Iterable[str | Path] | str | Path = (),
        data_paths: Iterable[str | Path] | str | Path = (),
        output_paths: Iterable[str | Path] | str | Path = (),
        stage_job_specs: Mapping[str, Mapping[str, Any]] | None = None,
        estimated_cost: float | None = None,
        cost_limit: float | None = None,
        risk_level: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ExperimentProposal:
        self.policy.require(Action.PROPOSAL_CREATE)
        normalized_title = str(title).strip()
        if not normalized_title:
            raise ValueError("experiment proposal title cannot be empty")
        normalized_risk = str(risk_level).strip() if risk_level is not None else None
        if contains_secret(
            {"title": normalized_title, "risk_level": normalized_risk}
        ):
            raise ValueError("experiment proposal must not contain credentials")
        if cost_limit is not None and (
            isinstance(cost_limit, bool)
            or not math.isfinite(float(cost_limit))
            or float(cost_limit) <= 0
        ):
            raise ValueError("experiment cost_limit must be finite and positive")
        normalized_command = _normalize_command(command)
        normalized_code = _normalize_paths(code_paths)
        normalized_config = _normalize_paths(config_paths)
        normalized_data = _normalize_paths(data_paths)
        normalized_outputs = _normalize_paths(output_paths)
        normalized_metadata = _json_mapping(metadata)
        if normalized_command:
            compute_binding = normalized_metadata.get("compute_binding")
            if isinstance(compute_binding, Mapping):
                bound_spec = compute_binding.get("job_spec")
                if not isinstance(bound_spec, Mapping):
                    raise ExperimentIntegrityError(
                        "compute binding is missing its job specification"
                    )
                canonical_spec = JobSpec.from_dict(bound_spec)
                if tuple(canonical_spec.command) != normalized_command:
                    raise ExperimentIntegrityError(
                        "proposal command does not match its compute binding"
                    )
                bound_outputs = _normalize_paths(canonical_spec.outputs)
                if normalized_outputs and normalized_outputs != bound_outputs:
                    raise ExperimentIntegrityError(
                        "proposal outputs do not match its compute binding"
                    )
                normalized_outputs = bound_outputs
                normalized_metadata["execution_binding"] = dict(compute_binding)
            else:
                canonical_spec, execution_binding = build_compute_binding(
                    self.workspace,
                    job_spec={
                        "name": "experiment-proposal",
                        "command": list(normalized_command),
                        "workdir": ".",
                        "inputs": [
                            *normalized_code,
                            *normalized_config,
                            *normalized_data,
                        ],
                        "outputs": list(normalized_outputs),
                        "metadata": (
                            {"estimated_cost": estimated_cost}
                            if estimated_cost is not None
                            else {}
                        ),
                        "execute": True,
                    },
                    compute_backend="local",
                    compute_config={},
                )
                normalized_command = tuple(canonical_spec.command)
                normalized_metadata["execution_binding"] = execution_binding
        if stage_job_specs:
            stage_bindings: dict[str, dict[str, Any]] = {}
            for raw_stage, raw_spec in stage_job_specs.items():
                stage = _normalize_stage(raw_stage)
                if stage not in {
                    ExperimentStage.STATIC_CHECK,
                    ExperimentStage.MINI_EXPERIMENT,
                }:
                    raise ExperimentIntegrityError(
                        "only static and mini stage job specs are accepted"
                    )
                stage_spec, stage_binding = build_compute_binding(
                    self.workspace,
                    job_spec=raw_spec,
                    compute_backend="local",
                    compute_config={},
                )
                if not stage_spec.execute:
                    raise ExperimentIntegrityError(
                        f"{stage.value} stage job must be executable"
                    )
                if tuple(stage_spec.command) == normalized_command:
                    raise ExperimentIntegrityError(
                        f"{stage.value} must use a distinct stage-limited command"
                    )
                stage_bindings[stage.value.lower()] = stage_binding
            normalized_metadata["experiment_stage_bindings"] = stage_bindings
        raw_stage_bindings = normalized_metadata.get(
            "experiment_stage_bindings"
        )
        if isinstance(raw_stage_bindings, Mapping):
            normalized_metadata["experiment_stage_bindings"] = (
                self._validate_stage_bindings(
                    raw_stage_bindings,
                    full_command=normalized_command,
                    require_complete=True,
                )
            )
        proposal_document = {
            "title": normalized_title,
            "command": list(normalized_command),
            "code_paths": list(normalized_code),
            "config_paths": list(normalized_config),
            "data_paths": list(normalized_data),
            "output_paths": list(normalized_outputs),
            "metadata": normalized_metadata,
        }
        proposal_id = _stable_id(
            "exp",
            str(self.workspace),
            json.dumps(
                proposal_document,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
        created_at = utc_now()
        with self.memory.connect() as db:
            db.execute(
                """
                INSERT INTO experiments
                (id, title, status, proposal_json, approved_at, cost_limit,
                 risk_level, created_at)
                VALUES (?, ?, ?, ?, NULL, ?, ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (
                    proposal_id,
                    normalized_title,
                    ExperimentStatus.PROPOSED.value,
                    json.dumps(
                        proposal_document,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    float(cost_limit) if cost_limit is not None else None,
                    normalized_risk,
                    created_at,
                ),
            )
        return self.get_proposal(proposal_id)

    create_proposal = propose

    def get_proposal(self, proposal_id: str) -> ExperimentProposal:
        with self.memory.connect() as db:
            row = db.execute(
                "SELECT * FROM experiments WHERE id = ?",
                (proposal_id,),
            ).fetchone()
        if row is None:
            raise KeyError(proposal_id)
        try:
            document = json.loads(row["proposal_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ExperimentIntegrityError(
                f"proposal record is not valid JSON: {proposal_id}"
            ) from exc
        return ExperimentProposal(
            proposal_id=str(row["id"]),
            title=str(row["title"]),
            status=ExperimentStatus(str(row["status"])),
            command=_normalize_command(document.get("command") or ()),
            code_paths=_normalize_paths(document.get("code_paths") or ()),
            config_paths=_normalize_paths(document.get("config_paths") or ()),
            data_paths=_normalize_paths(document.get("data_paths") or ()),
            output_paths=_normalize_paths(document.get("output_paths") or ()),
            cost_limit=float(row["cost_limit"]) if row["cost_limit"] is not None else None,
            risk_level=str(row["risk_level"]) if row["risk_level"] is not None else None,
            created_at=str(row["created_at"]),
            approved_at=(
                str(row["approved_at"]) if row["approved_at"] is not None else None
            ),
            metadata=dict(document.get("metadata") or {}),
        )

    get = get_proposal

    def approve(
        self,
        proposal_id: str,
        *,
        approved_by: str,
        scope: Mapping[str, Any] | None = None,
    ) -> str:
        self.get_proposal(proposal_id)
        normalized_approver = str(approved_by).strip()
        if not normalized_approver:
            raise ValueError("approved_by cannot be empty")
        if contains_secret(normalized_approver):
            raise ValueError("approved_by must not contain credentials")
        normalized_scope = _json_mapping(scope)
        proposal = self.get_proposal(proposal_id)
        execution_binding = proposal.metadata.get("execution_binding")
        if isinstance(execution_binding, Mapping):
            binding_sha256 = execution_binding.get("binding_sha256")
            supplied = normalized_scope.get("experiment_binding_sha256")
            if supplied is not None and supplied != binding_sha256:
                raise ValueError(
                    "approval scope cannot override experiment_binding_sha256"
                )
            normalized_scope["experiment_binding_sha256"] = binding_sha256
        stage_bindings = proposal.metadata.get("experiment_stage_bindings")
        if isinstance(stage_bindings, Mapping):
            stage_hashes = {
                str(stage): dict(binding).get("binding_sha256")
                for stage, binding in stage_bindings.items()
                if isinstance(binding, Mapping)
            }
            supplied_stage_hashes = normalized_scope.get(
                "experiment_stage_binding_sha256"
            )
            if (
                supplied_stage_hashes is not None
                and supplied_stage_hashes != stage_hashes
            ):
                raise ValueError(
                    "approval scope cannot override experiment stage bindings"
                )
            normalized_scope["experiment_stage_binding_sha256"] = stage_hashes
        approved_at = utc_now()
        approval_id = _stable_id(
            "approval",
            proposal_id,
            normalized_approver,
            json.dumps(normalized_scope, ensure_ascii=False, sort_keys=True),
        )
        with self.memory.connect() as db:
            db.execute(
                """
                UPDATE approvals
                SET status = 'SUPERSEDED'
                WHERE proposal_id = ? AND status = 'APPROVED'
                """,
                (proposal_id,),
            )
            db.execute(
                """
                INSERT OR REPLACE INTO approvals
                (id, proposal_id, status, approved_by, approved_at, scope_json)
                VALUES (?, ?, 'APPROVED', ?, ?, ?)
                """,
                (
                    approval_id,
                    proposal_id,
                    normalized_approver,
                    approved_at,
                    json.dumps(normalized_scope, ensure_ascii=False, sort_keys=True),
                ),
            )
            db.execute(
                """
                UPDATE experiments
                SET status = ?, approved_at = ?
                WHERE id = ?
                """,
                (ExperimentStatus.APPROVED.value, approved_at, proposal_id),
            )
        return approval_id

    def _require_approval(
        self,
        proposal_id: str,
        stage: ExperimentStage,
    ) -> dict[str, Any]:
        with self.memory.connect() as db:
            row = db.execute(
                """
                SELECT * FROM approvals
                WHERE proposal_id = ? AND status = 'APPROVED'
                ORDER BY approved_at DESC LIMIT 1
                """,
                (proposal_id,),
            ).fetchone()
        if row is None:
            raise PermissionError(f"proposal is not approved: {proposal_id}")
        scope = json.loads(row["scope_json"])
        scoped_proposal = scope.get("proposal_id") or scope.get("experiment_id")
        if scoped_proposal is not None and str(scoped_proposal) != proposal_id:
            raise PermissionError(f"approval scope does not cover proposal: {proposal_id}")
        if "stages" in scope:
            allowed_stages = {
                _normalize_stage(value) for value in scope.get("stages") or ()
            }
            if stage not in allowed_stages:
                raise PermissionError(
                    f"approval scope does not cover stage: {stage.value}"
                )
        elif "stage" in scope or "maximum_stage" in scope:
            maximum_stage = _normalize_stage(
                scope.get("maximum_stage", scope.get("stage"))
            )
            if _STAGE_SEQUENCE.index(stage) > _STAGE_SEQUENCE.index(maximum_stage):
                raise PermissionError(
                    f"approval scope does not cover stage: {stage.value}"
                )
        payload = dict(row)
        payload["scope"] = scope
        return payload

    def require_approval(
        self,
        proposal_id: str,
        stage: ExperimentStage | str = ExperimentStage.FULL_EXPERIMENT,
    ) -> dict[str, Any]:
        return self._require_approval(proposal_id, _normalize_stage(stage))

    def list_runs(self, proposal_id: str) -> list[ExperimentRunRecord]:
        self.get_proposal(proposal_id)
        with self.memory.connect() as db:
            rows = db.execute(
                """
                SELECT * FROM runs
                WHERE experiment_id = ?
                ORDER BY COALESCE(started_at, ended_at), id
                """,
                (proposal_id,),
            ).fetchall()
        return [self._run_from_row(row) for row in rows]

    runs = list_runs

    @staticmethod
    def _run_from_row(row: Any) -> ExperimentRunRecord:
        metadata = json.loads(row["metadata_json"])
        provenance = ProvenanceKind(metadata["provenance_kind"])
        return ExperimentRunRecord(
            run_id=str(row["id"]),
            proposal_id=str(row["experiment_id"]),
            stage=ExperimentStage(str(row["stage"])),
            status=ExperimentStatus(str(row["status"])),
            provenance_kind=provenance,
            eligible_for_claims=bool(metadata.get("eligible_for_claims", False)),
            code_sha256=row["code_sha256"],
            config_sha256=row["config_sha256"],
            data_sha256=row["data_sha256"],
            checkpoint_sha256=row["checkpoint_sha256"],
            metrics_sha256=row["metrics_sha256"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            metadata=metadata,
        )

    def get_run(self, run_id: str) -> ExperimentRunRecord:
        with self.memory.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return self._run_from_row(row)

    def import_metric_evidence(
        self,
        run_id: str,
        *,
        excerpt: str,
        path: str | None = None,
        line_start: int | None = None,
        line_end: int | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> str:
        run = self.get_run(run_id)
        if not run.eligible_for_claims or not run.metrics_sha256:
            raise ProvenanceViolation(
                "only execution-verified measured full runs can produce claim evidence"
            )
        normalized_excerpt = str(excerpt).strip()
        if not normalized_excerpt:
            raise ValueError("metric evidence excerpt cannot be empty")
        return self.memory.add_evidence(
            evidence_type="EXPERIMENT_METRIC",
            excerpt=normalized_excerpt,
            path=path,
            line_start=line_start,
            line_end=line_end,
            config_scope=f"experiment:{run.proposal_id}",
            metadata={
                **dict(metadata or {}),
                "run_id": run.run_id,
                "experiment_id": run.proposal_id,
                "execution_verified": True,
                "eligible_for_claims": True,
                "metrics_sha256": run.metrics_sha256,
                "code_sha256": run.code_sha256,
                "config_sha256": run.config_sha256,
                "data_sha256": run.data_sha256,
                "checkpoint_sha256": run.checkpoint_sha256,
            },
        )

    def next_stage(self, proposal_id: str) -> ExperimentStage | None:
        passed_stages = {
            run.stage
            for run in self.list_runs(proposal_id)
            if run.status is ExperimentStatus.PASSED
        }
        for stage in _STAGE_SEQUENCE:
            if stage not in passed_stages:
                return stage
        return None

    def _validate_transition(
        self,
        proposal_id: str,
        stage: ExperimentStage,
    ) -> None:
        expected = self.next_stage(proposal_id)
        if expected is None:
            raise ExperimentTransitionError(
                f"proposal already completed every stage: {proposal_id}"
            )
        if stage is not expected:
            raise ExperimentTransitionError(
                f"expected {expected.value}, received {stage.value}"
            )

    @classmethod
    def _reject_simulated_claims(
        cls,
        provenance_kind: ProvenanceKind,
        metadata: Mapping[str, Any],
    ) -> None:
        if provenance_kind is not ProvenanceKind.SIMULATED:
            return

        def walk(value: Any) -> bool:
            if isinstance(value, Mapping):
                for key, nested in value.items():
                    normalized_key = str(key).strip().lower().replace("-", "_")
                    if normalized_key in _SIMULATED_CLAIM_KEYS and nested not in (
                        None,
                        "",
                        [],
                        {},
                    ):
                        return True
                    if walk(nested):
                        return True
            elif isinstance(value, list | tuple):
                return any(walk(item) for item in value)
            return False

        if walk(metadata):
            raise ProvenanceViolation(
                "simulated outputs cannot contain performance or publication conclusions"
            )

    def _authorize_stage(
        self,
        proposal_id: str,
        stage: ExperimentStage,
    ) -> None:
        if self.profile is not ExecutionProfile.FULL:
            # Keep the policy's stable error contract rather than a second
            # profile-specific exception type.
            self.policy.require(_STAGE_ACTIONS[stage])
        self.policy.require(_STAGE_ACTIONS[stage])
        self._require_approval(proposal_id, stage)
        self._validate_transition(proposal_id, stage)

    def record_stage(
        self,
        proposal_id: str,
        stage: ExperimentStage | str,
        *,
        status: ExperimentStatus | str,
        provenance_kind: ProvenanceKind | str,
        code_paths: Iterable[str | Path] | str | Path | None = None,
        config_paths: Iterable[str | Path] | str | Path | None = None,
        data_paths: Iterable[str | Path] | str | Path | None = None,
        checkpoint_paths: Iterable[str | Path] | str | Path = (),
        metrics_paths: Iterable[str | Path] | str | Path = (),
        metadata: Mapping[str, Any] | None = None,
        started_at: str | None = None,
        ended_at: str | None = None,
        _verification: object | None = None,
        _run_id: str | None = None,
    ) -> ExperimentRunRecord:
        normalized_stage = _normalize_stage(stage)
        normalized_status = ExperimentStatus(status)
        normalized_provenance = ProvenanceKind(provenance_kind)
        if normalized_status not in _FINAL_RUN_STATUSES:
            raise ExperimentTransitionError(
                "record_stage requires PASSED, FAILED, or CANCELLED status"
            )
        proposal = self.get_proposal(proposal_id)
        self._authorize_stage(proposal_id, normalized_stage)
        normalized_metadata = _json_mapping(metadata)
        self._reject_simulated_claims(normalized_provenance, normalized_metadata)

        normalized_code = (
            proposal.code_paths if code_paths is None else _normalize_paths(code_paths)
        )
        normalized_config = (
            proposal.config_paths
            if config_paths is None
            else _normalize_paths(config_paths)
        )
        normalized_data = (
            proposal.data_paths if data_paths is None else _normalize_paths(data_paths)
        )
        normalized_checkpoints = _normalize_paths(checkpoint_paths)
        normalized_metrics = _normalize_paths(metrics_paths)
        code_hash, code_entries = self._hash_paths(normalized_code)
        config_hash, config_entries = self._hash_paths(normalized_config)
        data_hash, data_entries = self._hash_paths(normalized_data)
        checkpoint_hash, checkpoint_entries = self._hash_paths(normalized_checkpoints)
        metrics_hash, metrics_entries = self._hash_paths(normalized_metrics)

        eligible_for_claims = all(
            (
                _verification is _EXECUTION_VERIFIED,
                normalized_stage is ExperimentStage.FULL_EXPERIMENT,
                normalized_status is ExperimentStatus.PASSED,
                normalized_provenance is ProvenanceKind.MEASURED,
                code_hash is not None,
                config_hash is not None,
                data_hash is not None,
                metrics_hash is not None,
            )
        )
        now = utc_now()
        run_id = _run_id or f"run_{uuid.uuid4().hex}"
        provenance_metadata = {
            **normalized_metadata,
            "provenance_kind": normalized_provenance.value,
            "execution_verified": _verification is _EXECUTION_VERIFIED,
            "eligible_for_claims": eligible_for_claims,
            "hash_inputs": {
                "code": code_entries,
                "config": config_entries,
                "data": data_entries,
                "checkpoint": checkpoint_entries,
                "metrics": metrics_entries,
            },
        }
        with self.memory.connect() as db:
            db.execute(
                """
                INSERT INTO runs
                (id, experiment_id, stage, status, code_sha256, config_sha256,
                 data_sha256, checkpoint_sha256, metrics_sha256, started_at,
                 ended_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status=excluded.status,
                    code_sha256=excluded.code_sha256,
                    config_sha256=excluded.config_sha256,
                    data_sha256=excluded.data_sha256,
                    checkpoint_sha256=excluded.checkpoint_sha256,
                    metrics_sha256=excluded.metrics_sha256,
                    ended_at=excluded.ended_at,
                    metadata_json=excluded.metadata_json
                """,
                (
                    run_id,
                    proposal_id,
                    normalized_stage.value,
                    normalized_status.value,
                    code_hash,
                    config_hash,
                    data_hash,
                    checkpoint_hash,
                    metrics_hash,
                    started_at or now,
                    ended_at or now,
                    json.dumps(
                        provenance_metadata,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                ),
            )
            if normalized_status is ExperimentStatus.PASSED:
                proposal_status = (
                    ExperimentStatus.PASSED
                    if normalized_stage is ExperimentStage.FULL_EXPERIMENT
                    else ExperimentStatus.RUNNING
                )
            elif normalized_status is ExperimentStatus.CANCELLED:
                proposal_status = ExperimentStatus.CANCELLED
            else:
                proposal_status = ExperimentStatus.FAILED
            db.execute(
                "UPDATE experiments SET status = ? WHERE id = ?",
                (proposal_status.value, proposal_id),
            )
        return self.get_run(run_id)

    advance = record_stage

    @staticmethod
    def _default_executor(
        command: tuple[str, ...],
        workspace: Path,
        stage: ExperimentStage,
    ) -> ExecutionOutcome:
        del stage
        completed = subprocess.run(
            command,
            cwd=workspace,
            check=False,
            capture_output=True,
            text=True,
        )
        return ExecutionOutcome(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    @staticmethod
    def _normalize_outcome(
        value: ExecutionOutcome | subprocess.CompletedProcess[str] | Mapping[str, Any],
    ) -> ExecutionOutcome:
        if isinstance(value, ExecutionOutcome):
            return value
        if isinstance(value, subprocess.CompletedProcess):
            return ExecutionOutcome(
                returncode=int(value.returncode),
                stdout=str(value.stdout or ""),
                stderr=str(value.stderr or ""),
            )
        if isinstance(value, Mapping):
            return ExecutionOutcome(
                returncode=int(value.get("returncode", 0)),
                stdout=str(value.get("stdout") or ""),
                stderr=str(value.get("stderr") or ""),
                checkpoint_paths=_normalize_paths(value.get("checkpoint_paths") or ()),
                metrics_paths=_normalize_paths(value.get("metrics_paths") or ()),
                metadata=_json_mapping(value.get("metadata")),
            )
        raise TypeError("experiment executor returned an unsupported outcome")

    def _execute_bound_local_stage(
        self,
        execution_binding: Mapping[str, Any],
    ) -> ExecutionOutcome:
        if execution_binding.get("backend") != "local":
            raise ExperimentIntegrityError(
                "automatic experiment stages require a local immutable binding"
            )
        raw_spec = execution_binding.get("job_spec")
        execution_worktree = execution_binding.get("execution_worktree")
        if not isinstance(raw_spec, Mapping) or not isinstance(
            execution_worktree,
            str,
        ):
            raise ExperimentIntegrityError(
                "local experiment binding is missing its isolated worktree"
            )
        payload = dict(raw_spec)
        payload["workdir"] = execution_worktree
        payload["execute"] = True
        spec = JobSpec.from_dict(payload)
        backend = LocalBackend(
            policy=self.policy,
            state_dir=self.workspace / ".paperforge" / "compute",
        )
        result = backend.submit(spec)
        timeout = spec.resources.timeout_seconds or 300
        deadline = time.monotonic() + timeout
        while result.status in {
            JobStatus.SUBMITTED,
            JobStatus.QUEUED,
            JobStatus.RUNNING,
        }:
            if time.monotonic() >= deadline:
                backend.cancel(result.job_id, execute=True)
                raise TimeoutError("local experiment stage exceeded its time limit")
            time.sleep(0.02)
            result = backend.status(result.job_id, execute=True)
        logs = backend.logs(result.job_id, execute=True)
        if result.status is JobStatus.SUCCEEDED and spec.outputs:
            backend.sync_artifacts(
                result.job_id,
                self.workspace,
                direction=ArtifactDirection.DOWNLOAD,
                patterns=tuple(str(path) for path in spec.outputs),
                execute=True,
            )
        return ExecutionOutcome(
            returncode=result.return_code or (
                0 if result.status is JobStatus.SUCCEEDED else 1
            ),
            stdout=logs.stdout,
            stderr=logs.stderr,
            metadata={
                "backend": "local",
                "job_id": result.job_id,
                "job_status": result.status.value,
            },
        )

    @staticmethod
    def _estimated_cost(spec: JobSpec) -> float:
        raw = spec.metadata.get("estimated_cost")
        if (
            isinstance(raw, bool)
            or not isinstance(raw, int | float)
            or not math.isfinite(float(raw))
            or float(raw) <= 0
        ):
            raise ExperimentIntegrityError(
                "experiment stage requires a finite positive estimated_cost"
            )
        return float(raw)

    def _reserve_budget(
        self,
        proposal: ExperimentProposal,
        stage: ExperimentStage,
        spec: JobSpec,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[str, float]:
        if proposal.cost_limit is None or proposal.cost_limit <= 0:
            raise ExperimentIntegrityError(
                "experiment execution requires a positive cost_limit"
            )
        amount = self._estimated_cost(spec)
        event_id = f"budget_{uuid.uuid4().hex}"
        now = utc_now()
        with self.memory.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            spent = float(
                db.execute(
                    """
                    SELECT COALESCE(SUM(amount), 0)
                    FROM experiment_budget_events
                    WHERE experiment_id = ?
                      AND status IN ('RESERVED', 'CHARGED', 'VIOLATION')
                    """,
                    (proposal.proposal_id,),
                ).fetchone()[0]
            )
            if spent + amount > proposal.cost_limit:
                raise ExperimentIntegrityError(
                    "experiment stage would exceed the approved cost_limit"
                )
            db.execute(
                """
                INSERT INTO experiment_budget_events
                (id, experiment_id, stage, amount, status, created_at,
                 updated_at, metadata_json)
                VALUES (?, ?, ?, ?, 'RESERVED', ?, ?, ?)
                """,
                (
                    event_id,
                    proposal.proposal_id,
                    stage.value,
                    amount,
                    now,
                    now,
                    json.dumps(
                        _json_mapping(metadata),
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                ),
            )
        return event_id, amount

    def _settle_budget(
        self,
        event_id: str,
        *,
        status: str,
        amount: float,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        normalized_status = str(status).strip().upper()
        if normalized_status not in {"CHARGED", "RELEASED", "VIOLATION"}:
            raise ValueError("unsupported experiment budget status")
        with self.memory.connect() as db:
            db.execute(
                """
                UPDATE experiment_budget_events
                SET status = ?, amount = ?, updated_at = ?, metadata_json = ?
                WHERE id = ?
                """,
                (
                    normalized_status,
                    amount,
                    utc_now(),
                    json.dumps(
                        _json_mapping(metadata),
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    event_id,
                ),
            )

    def _authorized_stage_binding(
        self,
        proposal_id: str,
        stage: ExperimentStage,
        *,
        command: Sequence[str] | None = None,
    ) -> tuple[
        ExperimentProposal,
        Mapping[str, Any],
        JobSpec,
        tuple[str, ...],
    ]:
        proposal = self.get_proposal(proposal_id)
        stage_key = stage.value.lower()
        stage_bindings = proposal.metadata.get("experiment_stage_bindings")
        selected_binding = (
            dict(stage_bindings[stage_key])
            if stage is not ExperimentStage.FULL_EXPERIMENT
            and isinstance(stage_bindings, Mapping)
            and isinstance(stage_bindings.get(stage_key), Mapping)
            else proposal.metadata.get("execution_binding")
            if stage is ExperimentStage.FULL_EXPERIMENT
            else None
        )
        if not isinstance(selected_binding, Mapping):
            raise ExperimentIntegrityError(
                f"experiment stage {stage.value} requires its own "
                "immutable job binding"
            )
        selected_spec_payload = selected_binding.get("job_spec")
        if not isinstance(selected_spec_payload, Mapping):
            raise ExperimentIntegrityError(
                "experiment stage binding is missing its job specification"
            )
        selected_spec = JobSpec.from_dict(selected_spec_payload)
        if stage is not ExperimentStage.FULL_EXPERIMENT:
            self._validate_stage_bindings(
                {stage_key: selected_binding},
                full_command=proposal.command,
                require_complete=False,
            )
        normalized_command = (
            _normalize_command(command)
            if command is not None
            else tuple(selected_spec.command)
        )
        if not normalized_command:
            raise ValueError(
                "experiment execution requires an argument-vector command"
            )
        if tuple(normalized_command) != tuple(selected_spec.command):
            raise ExperimentIntegrityError(
                "experiment execution command does not match the approved stage"
            )
        approval = self._require_approval(proposal_id, stage)
        approval_scope = _json_mapping(approval.get("scope"))
        expected_binding_sha256 = selected_binding.get("binding_sha256")
        approved_binding_sha256 = (
            approval_scope.get("experiment_binding_sha256")
            if stage is ExperimentStage.FULL_EXPERIMENT
            else dict(
                approval_scope.get("experiment_stage_binding_sha256") or {}
            ).get(stage_key)
        )
        if approved_binding_sha256 != expected_binding_sha256:
            raise ExperimentIntegrityError(
                "experiment approval is not bound to the proposal inputs"
            )
        verified, detail = verify_compute_binding(
            self.workspace,
            selected_binding,
        )
        if not verified:
            raise ExperimentIntegrityError(
                f"experiment proposal binding changed: {detail}"
            )
        self._authorize_stage(proposal_id, stage)
        self.policy.validate_command(
            normalized_command,
            _STAGE_ACTIONS[stage],
        )
        return (
            proposal,
            selected_binding,
            selected_spec,
            normalized_command,
        )

    def reserve_backend_stage(
        self,
        proposal_id: str,
        stage: ExperimentStage | str,
        *,
        lifecycle_id: str,
    ) -> tuple[str, float]:
        normalized_stage = _normalize_stage(stage)
        lifecycle = str(lifecycle_id).strip()
        if not lifecycle or contains_secret(lifecycle):
            raise ExperimentIntegrityError(
                "backend lifecycle_id must be non-secret and non-empty"
            )
        proposal, binding, spec, _ = self._authorized_stage_binding(
            proposal_id,
            normalized_stage,
        )
        with self.memory.connect() as db:
            rows = db.execute(
                """
                SELECT id, amount, status, metadata_json
                FROM experiment_budget_events
                WHERE experiment_id = ? AND stage = ?
                ORDER BY created_at, id
                """,
                (proposal_id, normalized_stage.value),
            ).fetchall()
        original_output_hashes: Mapping[str, Any] | None = None
        for row in rows:
            metadata = json.loads(row["metadata_json"])
            if metadata.get("lifecycle_id") != lifecycle:
                continue
            if original_output_hashes is None and isinstance(
                metadata.get("output_hashes_before"),
                Mapping,
            ):
                original_output_hashes = dict(
                    metadata["output_hashes_before"]
                )
            if row["status"] in {"RESERVED", "CHARGED"}:
                return str(row["id"]), float(row["amount"])
        return self._reserve_budget(
            proposal,
            normalized_stage,
            spec,
            metadata={
                "lifecycle_id": lifecycle,
                "binding_sha256": binding.get("binding_sha256"),
                "job_id": spec.job_id,
                "output_hashes_before": (
                    dict(original_output_hashes)
                    if original_output_hashes is not None
                    else self._output_hashes(
                        _normalize_paths(spec.outputs)
                    )
                ),
            },
        )

    def finalize_backend_stage(
        self,
        proposal_id: str,
        stage: ExperimentStage | str,
        *,
        lifecycle_id: str,
        budget_event_id: str,
        returncode: int,
        stdout: str = "",
        stderr: str = "",
        actual_cost: float | None = None,
        checkpoint_paths: Iterable[str | Path] | str | Path = (),
        metrics_paths: Iterable[str | Path] | str | Path = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> ExperimentRunRecord:
        normalized_stage = _normalize_stage(stage)
        stable_run_id = _stable_id(
            "run",
            proposal_id,
            normalized_stage.value,
            lifecycle_id,
            budget_event_id,
        )
        try:
            existing_run = self.get_run(stable_run_id)
        except KeyError:
            existing_run = None
        if existing_run is not None:
            if (
                existing_run.proposal_id != proposal_id
                or existing_run.stage is not normalized_stage
                or existing_run.metadata.get("backend_lifecycle_id")
                != lifecycle_id
                or existing_run.metadata.get("budget_event_id")
                != budget_event_id
            ):
                raise ExperimentIntegrityError(
                    "backend run idempotency record does not match execution"
                )
            return existing_run
        proposal, binding, spec, normalized_command = (
            self._authorized_stage_binding(
                proposal_id,
                normalized_stage,
            )
        )
        with self.memory.connect() as db:
            row = db.execute(
                """
                SELECT * FROM experiment_budget_events
                WHERE id = ? AND experiment_id = ? AND stage = ?
                """,
                (
                    budget_event_id,
                    proposal_id,
                    normalized_stage.value,
                ),
            ).fetchone()
        if row is None:
            raise ExperimentIntegrityError(
                "backend stage budget reservation was not found"
            )
        budget_metadata = json.loads(row["metadata_json"])
        if (
            budget_metadata.get("lifecycle_id") != lifecycle_id
            or budget_metadata.get("binding_sha256")
            != binding.get("binding_sha256")
            or budget_metadata.get("job_id") != spec.job_id
            or row["status"] not in {"RESERVED", "CHARGED"}
        ):
            raise ExperimentIntegrityError(
                "backend stage reservation does not match execution"
            )
        estimated_cost = float(
            budget_metadata.get("estimated_cost", row["amount"])
        )
        charged_cost = (
            float(row["amount"])
            if row["status"] == "CHARGED" and actual_cost is None
            else estimated_cost
            if actual_cost is None
            else float(actual_cost)
        )
        if (
            not math.isfinite(charged_cost)
            or charged_cost < 0
            or charged_cost > estimated_cost
        ):
            self._settle_budget(
                budget_event_id,
                status="VIOLATION",
                amount=estimated_cost,
                metadata={"reason": "actual_cost_exceeded_reservation"},
            )
            raise ExperimentIntegrityError(
                "experiment actual_cost exceeded its reserved budget"
            )
        if row["status"] == "CHARGED" and charged_cost != float(row["amount"]):
            raise ExperimentIntegrityError(
                "backend stage actual_cost changed after it was charged"
            )
        normalized_checkpoints = _normalize_paths(checkpoint_paths)
        normalized_metrics = _normalize_paths(metrics_paths)
        if (
            normalized_stage is ExperimentStage.FULL_EXPERIMENT
            and returncode == 0
            and not normalized_metrics
        ):
            raise ExperimentIntegrityError(
                "successful full experiments require declared metrics evidence"
            )
        declared_outputs = set(_normalize_paths(spec.outputs))
        evidence_outputs = {
            *normalized_checkpoints,
            *normalized_metrics,
        }
        if not evidence_outputs.issubset(declared_outputs):
            raise ExperimentIntegrityError(
                "backend evidence paths must be declared job outputs"
            )
        before = {
            str(key): value
            for key, value in dict(
                budget_metadata.get("output_hashes_before") or {}
            ).items()
        }
        after = self._output_hashes(_normalize_paths(spec.outputs))
        fresh_outputs = sorted(
            path
            for path, digest in after.items()
            if digest is not None and digest != before.get(path)
        )
        evidence_verified = evidence_outputs.issubset(fresh_outputs)
        execution_verified = (
            returncode == 0
            and (
                normalized_stage is not ExperimentStage.FULL_EXPERIMENT
                or (
                    bool(normalized_metrics)
                    and evidence_verified
                )
            )
        )
        if row["status"] == "RESERVED":
            self._settle_budget(
                budget_event_id,
                status="CHARGED",
                amount=charged_cost,
                metadata={
                    **budget_metadata,
                    "estimated_cost": estimated_cost,
                },
            )
        normalized_metadata = {
            **_json_mapping(metadata),
            "backend_lifecycle_id": lifecycle_id,
            "binding_sha256": binding.get("binding_sha256"),
            "command": list(normalized_command),
            "returncode": returncode,
            "stdout_sha256": hashlib.sha256(
                stdout.encode("utf-8")
            ).hexdigest(),
            "stderr_sha256": hashlib.sha256(
                stderr.encode("utf-8")
            ).hexdigest(),
            "execution_verified": execution_verified,
            "fresh_outputs": fresh_outputs,
            "evidence_outputs_verified": evidence_verified,
            "budget_event_id": budget_event_id,
            "estimated_cost": estimated_cost,
            "actual_cost": charged_cost,
        }
        return self.record_stage(
            proposal_id,
            normalized_stage,
            status=(
                ExperimentStatus.PASSED
                if returncode == 0
                else ExperimentStatus.FAILED
            ),
            provenance_kind=ProvenanceKind.MEASURED,
            checkpoint_paths=(
                normalized_checkpoints if returncode == 0 else ()
            ),
            metrics_paths=normalized_metrics if returncode == 0 else (),
            metadata=normalized_metadata,
            started_at=str(row["created_at"]),
            ended_at=utc_now(),
            _verification=(
                _EXECUTION_VERIFIED if execution_verified else None
            ),
            _run_id=stable_run_id,
        )

    def charge_backend_stage(
        self,
        proposal_id: str,
        stage: ExperimentStage | str,
        *,
        lifecycle_id: str,
        budget_event_id: str,
        actual_cost: float | None = None,
    ) -> float:
        """Durably charge a terminal backend execution before artifact handling."""

        normalized_stage = _normalize_stage(stage)
        with self.memory.connect() as db:
            existing_row = db.execute(
                """
                SELECT * FROM experiment_budget_events
                WHERE id = ? AND experiment_id = ? AND stage = ?
                """,
                (
                    budget_event_id,
                    proposal_id,
                    normalized_stage.value,
                ),
            ).fetchone()
        if existing_row is None:
            raise ExperimentIntegrityError(
                "backend stage budget reservation was not found"
            )
        existing_metadata = json.loads(existing_row["metadata_json"])
        if existing_metadata.get("lifecycle_id") != lifecycle_id:
            raise ExperimentIntegrityError(
                "backend stage reservation does not match execution"
            )
        if existing_row["status"] == "CHARGED":
            charged_cost = float(existing_row["amount"])
            if actual_cost is not None and float(actual_cost) != charged_cost:
                raise ExperimentIntegrityError(
                    "backend stage actual_cost changed after it was charged"
                )
            return charged_cost
        _, binding, spec, _ = self._authorized_stage_binding(
            proposal_id,
            normalized_stage,
        )
        with self.memory.connect() as db:
            row = db.execute(
                """
                SELECT * FROM experiment_budget_events
                WHERE id = ? AND experiment_id = ? AND stage = ?
                """,
                (
                    budget_event_id,
                    proposal_id,
                    normalized_stage.value,
                ),
            ).fetchone()
        if row is None:
            raise ExperimentIntegrityError(
                "backend stage budget reservation was not found"
            )
        metadata = json.loads(row["metadata_json"])
        if (
            metadata.get("lifecycle_id") != lifecycle_id
            or metadata.get("binding_sha256") != binding.get("binding_sha256")
            or metadata.get("job_id") != spec.job_id
        ):
            raise ExperimentIntegrityError(
                "backend stage reservation does not match execution"
            )
        estimated_cost = float(metadata.get("estimated_cost", row["amount"]))
        charged_cost = estimated_cost if actual_cost is None else float(actual_cost)
        if (
            not math.isfinite(charged_cost)
            or charged_cost < 0
            or charged_cost > estimated_cost
        ):
            if row["status"] == "RESERVED":
                self._settle_budget(
                    budget_event_id,
                    status="VIOLATION",
                    amount=estimated_cost,
                    metadata={
                        **metadata,
                        "estimated_cost": estimated_cost,
                        "reason": "actual_cost_exceeded_reservation",
                    },
                )
            raise ExperimentIntegrityError(
                "experiment actual_cost exceeded its reserved budget"
            )
        if row["status"] != "RESERVED":
            raise ExperimentIntegrityError(
                "backend stage reservation cannot be charged from its current state"
            )
        self._settle_budget(
            budget_event_id,
            status="CHARGED",
            amount=charged_cost,
            metadata={
                **metadata,
                "estimated_cost": estimated_cost,
                "execution_terminal": True,
                "execution_charged_at": utc_now(),
            },
        )
        return charged_cost

    def release_backend_stage(
        self,
        proposal_id: str,
        *,
        lifecycle_id: str,
        budget_event_id: str,
        reason: str,
    ) -> None:
        with self.memory.connect() as db:
            row = db.execute(
                """
                SELECT amount, status, metadata_json
                FROM experiment_budget_events
                WHERE id = ? AND experiment_id = ?
                """,
                (budget_event_id, proposal_id),
            ).fetchone()
        if row is None:
            raise ExperimentIntegrityError(
                "backend stage budget reservation was not found"
            )
        metadata = json.loads(row["metadata_json"])
        if metadata.get("lifecycle_id") != lifecycle_id:
            raise ExperimentIntegrityError(
                "backend stage reservation does not match execution"
            )
        if row["status"] == "RESERVED":
            self._settle_budget(
                budget_event_id,
                status="RELEASED",
                amount=float(row["amount"]),
                metadata={
                    **metadata,
                    "reason": str(reason).strip() or "backend_stage_released",
                },
            )

    def execute_stage(
        self,
        proposal_id: str,
        stage: ExperimentStage | str,
        *,
        provenance_kind: ProvenanceKind | str = ProvenanceKind.MEASURED,
        command: Sequence[str] | None = None,
        executor: ExperimentExecutor | None = None,
        code_paths: Iterable[str | Path] | str | Path | None = None,
        config_paths: Iterable[str | Path] | str | Path | None = None,
        data_paths: Iterable[str | Path] | str | Path | None = None,
        checkpoint_paths: Iterable[str | Path] | str | Path = (),
        metrics_paths: Iterable[str | Path] | str | Path = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> ExperimentRunRecord:
        normalized_stage = _normalize_stage(stage)
        (
            proposal,
            selected_binding,
            selected_spec,
            normalized_command,
        ) = self._authorized_stage_binding(
            proposal_id,
            normalized_stage,
            command=command,
        )
        selected_outputs = _normalize_paths(selected_spec.outputs)
        output_hashes_before = self._output_hashes(selected_outputs)
        budget_event_id, estimated_cost = self._reserve_budget(
            proposal,
            normalized_stage,
            selected_spec,
        )
        started_at = utc_now()
        try:
            if executor is None:
                outcome = self._execute_bound_local_stage(selected_binding)
                execution_verified = True
            else:
                outcome = self._normalize_outcome(
                    executor(
                        normalized_command,
                        self.workspace,
                        normalized_stage,
                    )
                )
                execution_verified = False
        except Exception:
            self._settle_budget(
                budget_event_id,
                status="RELEASED",
                amount=estimated_cost,
                metadata={"reason": "execution_failed_before_result"},
            )
            raise
        raw_actual_cost = outcome.metadata.get(
            "actual_cost",
            estimated_cost,
        )
        if (
            isinstance(raw_actual_cost, bool)
            or not isinstance(raw_actual_cost, int | float)
            or not math.isfinite(float(raw_actual_cost))
            or float(raw_actual_cost) < 0
            or float(raw_actual_cost) > estimated_cost
        ):
            self._settle_budget(
                budget_event_id,
                status="VIOLATION",
                amount=estimated_cost,
                metadata={"reason": "actual_cost_exceeded_reservation"},
            )
            raise ExperimentIntegrityError(
                "experiment actual_cost exceeded its reserved budget"
            )
        actual_cost = float(raw_actual_cost)
        self._settle_budget(
            budget_event_id,
            status="CHARGED",
            amount=actual_cost,
            metadata={"estimated_cost": estimated_cost},
        )
        effective_checkpoints = (
            _normalize_paths(checkpoint_paths) or outcome.checkpoint_paths
        )
        effective_metrics = _normalize_paths(metrics_paths) or outcome.metrics_paths
        if outcome.returncode != 0:
            effective_checkpoints = ()
            effective_metrics = ()
        output_hashes_after = self._output_hashes(selected_outputs)
        fresh_outputs = sorted(
            path
            for path, after in output_hashes_after.items()
            if after is not None and after != output_hashes_before.get(path)
        )
        evidence_outputs = {*effective_checkpoints, *effective_metrics}
        evidence_outputs_verified = evidence_outputs.issubset(fresh_outputs)
        if normalized_stage is ExperimentStage.FULL_EXPERIMENT:
            execution_verified = execution_verified and evidence_outputs_verified
        normalized_metadata = {
            **_json_mapping(metadata),
            **_json_mapping(outcome.metadata),
            "command": list(normalized_command),
            "returncode": outcome.returncode,
            "stdout_sha256": hashlib.sha256(
                outcome.stdout.encode("utf-8")
            ).hexdigest(),
            "stderr_sha256": hashlib.sha256(
                outcome.stderr.encode("utf-8")
            ).hexdigest(),
            "execution_verified": execution_verified,
            "fresh_outputs": fresh_outputs,
            "evidence_outputs_verified": evidence_outputs_verified,
            "budget_event_id": budget_event_id,
            "estimated_cost": estimated_cost,
            "actual_cost": actual_cost,
        }
        return self.record_stage(
            proposal_id,
            normalized_stage,
            status=(
                ExperimentStatus.PASSED
                if outcome.returncode == 0
                else ExperimentStatus.FAILED
            ),
            provenance_kind=provenance_kind,
            code_paths=code_paths,
            config_paths=config_paths,
            data_paths=data_paths,
            checkpoint_paths=effective_checkpoints,
            metrics_paths=effective_metrics,
            metadata=normalized_metadata,
            started_at=started_at,
            ended_at=utc_now(),
            _verification=(
                _EXECUTION_VERIFIED if execution_verified else None
            ),
        )

    execute = execute_stage


# Explicit engine name for service/CLI wiring.
ExperimentEngine = ExperimentManager


__all__ = [
    "ExecutionOutcome",
    "ExperimentEngine",
    "ExperimentError",
    "ExperimentExecutor",
    "ExperimentIntegrityError",
    "ExperimentManager",
    "ExperimentProposal",
    "ExperimentRun",
    "ExperimentRunRecord",
    "ExperimentTransitionError",
    "ProvenanceKind",
    "ProvenanceViolation",
]
