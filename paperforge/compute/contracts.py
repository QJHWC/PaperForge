from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JobStatus(str, Enum):
    PLANNED = "PLANNED"
    DRY_RUN = "PLANNED"
    SUBMITTED = "SUBMITTED"
    PENDING = "SUBMITTED"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUSPENDED = "SUSPENDED"
    SUCCEEDED = "SUCCEEDED"
    COMPLETED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    CANCELED = "CANCELLED"
    UNKNOWN = "UNKNOWN"

    @property
    def terminal(self) -> bool:
        return self in {
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }


class ArtifactDirection(str, Enum):
    DOWNLOAD = "download"
    UPLOAD = "upload"


_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_ENV_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SECRET_ENV_PATTERN = re.compile(
    r"(?:^|_)(?:API_?KEY|AUTH|COOKIE|CREDENTIAL|PASSWORD|SECRET|TOKEN)(?:_|$)",
    re.IGNORECASE,
)
_BROAD_ARTIFACT_PATTERNS = frozenset({".", "./", "*", "**", "**/*"})


@dataclass(frozen=True)
class ResourceSpec:
    cpus: int = 1
    memory_mb: int | None = None
    gpus: int = 0
    timeout_seconds: int | None = None
    queue: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.cpus, bool) or not isinstance(self.cpus, int) or self.cpus < 1:
            raise ValueError("resources.cpus must be a positive integer")
        if self.memory_mb is not None and (
            isinstance(self.memory_mb, bool)
            or not isinstance(self.memory_mb, int)
            or self.memory_mb < 1
        ):
            raise ValueError("resources.memory_mb must be a positive integer")
        if isinstance(self.gpus, bool) or not isinstance(self.gpus, int) or self.gpus < 0:
            raise ValueError("resources.gpus must be a non-negative integer")
        if self.timeout_seconds is not None and (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, int)
            or self.timeout_seconds < 1
        ):
            raise ValueError("resources.timeout_seconds must be a positive integer")
        if self.queue is not None and not _NAME_PATTERN.fullmatch(self.queue):
            raise ValueError("resources.queue contains unsafe characters")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _normalize_command(command: Sequence[str] | str) -> tuple[str, ...]:
    if isinstance(command, str):
        raise TypeError("command must be an argument sequence, not a shell string")
    normalized = tuple(os.fspath(part) for part in command)
    if not normalized:
        raise ValueError("command must contain at least one argument")
    if any(not part or "\x00" in part for part in normalized):
        raise ValueError("command arguments must be non-empty and contain no NUL bytes")
    return normalized


def _normalize_relative_artifact(path: str | Path) -> str:
    raw = os.fspath(path).replace("\\", "/")
    pure = PurePosixPath(raw)
    if not raw or pure.is_absolute() or ".." in pure.parts:
        raise ValueError(f"artifact path must be relative and traversal-free: {raw!r}")
    if raw in _BROAD_ARTIFACT_PATTERNS or pure.as_posix() in _BROAD_ARTIFACT_PATTERNS:
        raise ValueError(f"artifact path is too broad: {raw!r}")
    if "\x00" in raw:
        raise ValueError("artifact path contains a NUL byte")
    return pure.as_posix()


def _redacted_environment(env: Mapping[str, str]) -> dict[str, str]:
    return {
        key: "***" if _SECRET_ENV_PATTERN.search(key) else value
        for key, value in env.items()
    }


@dataclass(frozen=True)
class JobSpec:
    name: str
    command: Sequence[str]
    workdir: str | Path = "."
    env: Mapping[str, str] = field(default_factory=dict)
    inputs: Sequence[str | Path] = field(default_factory=tuple)
    outputs: Sequence[str | Path] = field(default_factory=tuple)
    resources: ResourceSpec = field(default_factory=ResourceSpec)
    backend_options: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    execute: bool = False
    job_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _NAME_PATTERN.fullmatch(self.name):
            raise ValueError(
                "name must start with an alphanumeric character and contain only "
                "letters, numbers, '.', '_' or '-'"
            )
        command = _normalize_command(self.command)
        workdir = os.fspath(self.workdir)
        if not workdir or "\x00" in workdir:
            raise ValueError("workdir must be a non-empty path without NUL bytes")

        env = dict(self.env)
        for key, value in env.items():
            if not isinstance(key, str) or not _ENV_PATTERN.fullmatch(key):
                raise ValueError(f"invalid environment variable name: {key!r}")
            if not isinstance(value, str) or "\x00" in value:
                raise ValueError(f"environment value for {key!r} must be a safe string")

        inputs = tuple(os.fspath(path) for path in self.inputs)
        if any(not path or "\x00" in path for path in inputs):
            raise ValueError("input paths must be non-empty and contain no NUL bytes")
        outputs = tuple(_normalize_relative_artifact(path) for path in self.outputs)
        resources = self.resources
        if isinstance(resources, Mapping):
            resources = ResourceSpec(**resources)
        if not isinstance(resources, ResourceSpec):
            raise TypeError("resources must be a ResourceSpec or mapping")
        if self.job_id is not None and not _NAME_PATTERN.fullmatch(self.job_id):
            raise ValueError("job_id contains unsafe characters")
        if not isinstance(self.execute, bool):
            raise TypeError("execute must be a boolean")

        object.__setattr__(self, "command", command)
        object.__setattr__(self, "workdir", workdir)
        object.__setattr__(self, "env", env)
        object.__setattr__(self, "inputs", inputs)
        object.__setattr__(self, "outputs", outputs)
        object.__setattr__(self, "resources", resources)
        object.__setattr__(self, "backend_options", dict(self.backend_options))
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def fingerprint(self) -> str:
        payload = self.to_dict()
        payload.pop("execute", None)
        payload.pop("job_id", None)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @property
    def argv(self) -> tuple[str, ...]:
        return tuple(self.command)

    @property
    def dry_run(self) -> bool:
        return not self.execute

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> JobSpec:
        data = dict(payload)
        resources = data.get("resources")
        if isinstance(resources, Mapping):
            data["resources"] = ResourceSpec(**resources)
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "command": list(self.command),
            "workdir": os.fspath(self.workdir),
            "env": _redacted_environment(self.env),
            "inputs": list(self.inputs),
            "outputs": list(self.outputs),
            "resources": self.resources.to_dict(),
            "backend_options": dict(self.backend_options),
            "metadata": dict(self.metadata),
            "execute": self.execute,
            "job_id": self.job_id,
        }


@dataclass(frozen=True)
class CommandPlan:
    action: str
    argv: tuple[str, ...]
    description: str
    cwd: str | None = None
    environment_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "argv", tuple(self.argv))
        object.__setattr__(self, "environment_keys", tuple(self.environment_keys))

    @property
    def command(self) -> tuple[str, ...]:
        return self.argv

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "argv": list(self.argv),
            "description": self.description,
            "cwd": self.cwd,
            "environment_keys": list(self.environment_keys),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> CommandPlan:
        return cls(
            action=str(payload["action"]),
            argv=tuple(str(part) for part in payload.get("argv") or ()),
            description=str(payload.get("description") or ""),
            cwd=str(payload["cwd"]) if payload.get("cwd") is not None else None,
            environment_keys=tuple(
                str(key) for key in payload.get("environment_keys") or ()
            ),
        )


@dataclass(frozen=True)
class ArtifactRecord:
    path: str
    size_bytes: int
    sha256: str | None = None

    def __post_init__(self) -> None:
        if self.size_bytes < 0:
            raise ValueError("size_bytes cannot be negative")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ArtifactRecord:
        return cls(
            path=str(payload["path"]),
            size_bytes=int(payload["size_bytes"]),
            sha256=(
                str(payload["sha256"])
                if payload.get("sha256") is not None
                else None
            ),
        )


@dataclass(frozen=True)
class JobResult:
    job_id: str
    backend: str
    status: JobStatus
    executed: bool
    plan: CommandPlan | None = None
    return_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    message: str = ""
    artifacts: tuple[ArtifactRecord, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not isinstance(self.status, JobStatus):
            object.__setattr__(self, "status", JobStatus(self.status))
        object.__setattr__(self, "artifacts", tuple(self.artifacts))
        object.__setattr__(self, "metadata", dict(self.metadata))
        if self.return_code is not None and isinstance(self.return_code, bool):
            raise TypeError("return_code must be an integer or None")

    @property
    def state(self) -> JobStatus:
        return self.status

    @property
    def returncode(self) -> int | None:
        return self.return_code

    @property
    def success(self) -> bool:
        return self.status is JobStatus.SUCCEEDED

    @property
    def dry_run(self) -> bool:
        return not self.executed

    @property
    def command(self) -> tuple[str, ...]:
        return self.plan.argv if self.plan else ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "backend": self.backend,
            "status": self.status.value,
            "executed": self.executed,
            "plan": self.plan.to_dict() if self.plan else None,
            "return_code": self.return_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "message": self.message,
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> JobResult:
        plan = payload.get("plan")
        return cls(
            job_id=str(payload["job_id"]),
            backend=str(payload["backend"]),
            status=JobStatus(str(payload["status"])),
            executed=bool(payload["executed"]),
            plan=CommandPlan.from_dict(plan) if isinstance(plan, Mapping) else None,
            return_code=(
                int(payload["return_code"])
                if payload.get("return_code") is not None
                else None
            ),
            stdout=str(payload.get("stdout") or ""),
            stderr=str(payload.get("stderr") or ""),
            message=str(payload.get("message") or ""),
            artifacts=tuple(
                ArtifactRecord.from_dict(item)
                for item in payload.get("artifacts") or ()
            ),
            metadata=dict(payload.get("metadata") or {}),
            created_at=str(payload.get("created_at") or utc_now()),
            updated_at=str(payload.get("updated_at") or utc_now()),
        )


def require_finite_number(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{field_name} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field_name} must be finite")
    return number
