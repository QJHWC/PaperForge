from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

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
    return normalized


def _json_mapping(value: Mapping[str, Any] | None) -> dict[str, Any]:
    normalized = dict(value or {})
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
            self.workspace / ".paperforge" / "paperforge.db"
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

    def propose(
        self,
        *,
        title: str,
        command: Sequence[str] | None = None,
        code_paths: Iterable[str | Path] | str | Path = (),
        config_paths: Iterable[str | Path] | str | Path = (),
        data_paths: Iterable[str | Path] | str | Path = (),
        cost_limit: float | None = None,
        risk_level: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ExperimentProposal:
        self.policy.require(Action.PROPOSAL_CREATE)
        normalized_title = str(title).strip()
        if not normalized_title:
            raise ValueError("experiment proposal title cannot be empty")
        if cost_limit is not None and (
            not math.isfinite(float(cost_limit)) or float(cost_limit) < 0
        ):
            raise ValueError("experiment cost_limit must be finite and non-negative")
        normalized_command = _normalize_command(command)
        normalized_code = _normalize_paths(code_paths)
        normalized_config = _normalize_paths(config_paths)
        normalized_data = _normalize_paths(data_paths)
        normalized_metadata = _json_mapping(metadata)
        proposal_document = {
            "title": normalized_title,
            "command": list(normalized_command),
            "code_paths": list(normalized_code),
            "config_paths": list(normalized_config),
            "data_paths": list(normalized_data),
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
                    str(risk_level) if risk_level is not None else None,
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
        normalized_scope = _json_mapping(scope)
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
        run_id = f"run_{uuid.uuid4().hex}"
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
        proposal = self.get_proposal(proposal_id)
        normalized_command = _normalize_command(command) if command is not None else proposal.command
        if not normalized_command:
            raise ValueError("experiment execution requires an argument-vector command")
        # Authorization and transition checks happen before invoking any code.
        self._authorize_stage(proposal_id, normalized_stage)
        self.policy.validate_command(
            normalized_command,
            _STAGE_ACTIONS[normalized_stage],
        )
        started_at = utc_now()
        outcome = self._normalize_outcome(
            (executor or self._default_executor)(
                normalized_command,
                self.workspace,
                normalized_stage,
            )
        )
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
            "execution_verified": True,
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
            checkpoint_paths=(
                _normalize_paths(checkpoint_paths) or outcome.checkpoint_paths
            ),
            metrics_paths=_normalize_paths(metrics_paths) or outcome.metrics_paths,
            metadata=normalized_metadata,
            started_at=started_at,
            ended_at=utc_now(),
            _verification=_EXECUTION_VERIFIED,
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
