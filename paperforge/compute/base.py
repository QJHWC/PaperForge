from __future__ import annotations

import os
import re
import subprocess
import threading
import uuid
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from engine.secret_redaction import redact_secrets
from paperforge.policy import Action, ExecutionPolicy

from .contracts import (
    ArtifactDirection,
    CommandPlan,
    JobResult,
    JobSpec,
    JobStatus,
)
from .state import ComputeStateStore


class UnknownJobError(KeyError):
    pass


class JobStateError(RuntimeError):
    pass


class SensitiveEnvironmentError(ValueError):
    pass


@dataclass(frozen=True)
class CommandOutcome:
    return_code: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int | None = None,
    ) -> CommandOutcome: ...


class SubprocessCommandRunner:
    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int | None = None,
    ) -> CommandOutcome:
        completed = subprocess.run(
            list(argv),
            cwd=os.fspath(cwd) if cwd is not None else None,
            env=dict(env) if env is not None else None,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return CommandOutcome(
            return_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


class ComputeBackend(ABC):
    name = "abstract"
    policy_action = Action.REMOTE_EXECUTE
    _SECRET_ENV_PATTERN = re.compile(
        r"(?:^|_)(?:API_?KEY|AUTH|COOKIE|CREDENTIAL|PASSWORD|SECRET|TOKEN)(?:_|$)",
        re.IGNORECASE,
    )

    def __init__(
        self,
        *,
        policy: ExecutionPolicy | None = None,
        runner: CommandRunner | None = None,
        state_dir: str | Path | None = None,
    ) -> None:
        self.policy = policy or ExecutionPolicy.from_value(None)
        self.runner = runner or SubprocessCommandRunner()
        self.state_dir = Path(state_dir or Path.cwd() / ".paperforge" / "compute")
        self._state = ComputeStateStore(self.state_dir)
        self._specs: dict[str, JobSpec] = {}
        self._results: dict[str, JobResult] = {}
        self._lock = threading.RLock()

    def _job_id(self, spec: JobSpec) -> str:
        return spec.job_id or f"{spec.name}-{uuid.uuid4().hex[:12]}"

    @staticmethod
    def _should_execute(spec: JobSpec, execute: bool | None) -> bool:
        return spec.execute if execute is None else execute

    def _remember(
        self,
        job_id: str,
        *,
        spec: JobSpec | None = None,
        result: JobResult | None = None,
    ) -> None:
        with self._lock:
            if spec is not None:
                self._specs[job_id] = spec
            if result is not None:
                self._results[job_id] = result
            if result is not None and result.executed:
                durable_spec = spec or self._specs.get(job_id)
                if durable_spec is not None:
                    self._state.save(self.name, job_id, durable_spec, result)

    def _known_spec(self, job_id: str) -> JobSpec:
        try:
            return self._specs[job_id]
        except KeyError as exc:
            persisted = self._state.load_spec(self.name, job_id)
            if persisted is None:
                raise UnknownJobError(f"unknown {self.name} job: {job_id}") from exc
            self._specs[job_id] = persisted
            return persisted

    def _known_result(self, job_id: str) -> JobResult:
        try:
            return self._results[job_id]
        except KeyError as exc:
            persisted = self._state.load_result(self.name, job_id)
            if persisted is None:
                raise UnknownJobError(f"unknown {self.name} job: {job_id}") from exc
            self._results[job_id] = persisted
            return persisted

    def _plan(
        self,
        *,
        job_id: str,
        action: str,
        argv: Sequence[str],
        description: str,
        cwd: str | Path | None = None,
        environment_keys: Sequence[str] = (),
        metadata: Mapping[str, object] | None = None,
        sensitive_values: Sequence[str] = (),
    ) -> JobResult:
        secrets = tuple(
            sorted(
                (value for value in sensitive_values if value),
                key=len,
                reverse=True,
            )
        )

        def redact(value: object) -> object:
            if isinstance(value, str):
                for secret in secrets:
                    value = value.replace(secret, "***")
                return value
            if isinstance(value, Mapping):
                return {str(key): redact(item) for key, item in value.items()}
            if isinstance(value, list | tuple):
                return [redact(item) for item in value]
            return value

        redacted_metadata = redact(dict(metadata or {}))
        assert isinstance(redacted_metadata, Mapping)
        return JobResult(
            job_id=job_id,
            backend=self.name,
            status=JobStatus.PLANNED,
            executed=False,
            plan=CommandPlan(
                action=action,
                argv=tuple(str(redact(os.fspath(part))) for part in argv),
                description=description,
                cwd=os.fspath(cwd) if cwd is not None else None,
                environment_keys=tuple(sorted(environment_keys)),
            ),
            metadata=redacted_metadata,
        )

    def _sensitive_environment_values(self, env: Mapping[str, str]) -> tuple[str, ...]:
        return tuple(value for key, value in env.items() if self._SECRET_ENV_PATTERN.search(key))

    def _reject_sensitive_remote_environment(self, spec: JobSpec) -> None:
        sensitive_keys = sorted(
            key for key in spec.env if self._SECRET_ENV_PATTERN.search(key)
        )
        if sensitive_keys:
            joined = ", ".join(sensitive_keys)
            raise SensitiveEnvironmentError(
                f"{self.name} execution refuses sensitive environment variables "
                f"because they could enter process arguments or remote metadata: {joined}"
            )

    def _run(
        self,
        spec: JobSpec,
        argv: Sequence[str],
        *,
        action: Action | None = None,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int | None = None,
    ) -> CommandOutcome:
        policy_action = action or self.policy_action
        self.policy.validate_command(tuple(argv), policy_action)
        outcome = self.runner.run(argv, cwd=cwd, env=env, timeout=timeout)
        secret_values = self._sensitive_environment_values(spec.env)
        return CommandOutcome(
            return_code=outcome.return_code,
            stdout=redact_secrets(outcome.stdout, secret_values=secret_values),
            stderr=redact_secrets(outcome.stderr, secret_values=secret_values),
        )

    @abstractmethod
    def submit(self, spec: JobSpec, *, execute: bool | None = None) -> JobResult: ...

    @abstractmethod
    def status(self, job_id: str, *, execute: bool = False) -> JobResult: ...

    @abstractmethod
    def cancel(self, job_id: str, *, execute: bool = False) -> JobResult: ...

    @abstractmethod
    def resume(self, job_id: str, *, execute: bool = False) -> JobResult: ...

    @abstractmethod
    def logs(
        self,
        job_id: str,
        *,
        tail: int | None = None,
        follow: bool = False,
        execute: bool = False,
    ) -> JobResult: ...

    @abstractmethod
    def sync_artifacts(
        self,
        job_id: str,
        local_path: str | Path,
        *,
        direction: ArtifactDirection | str = ArtifactDirection.DOWNLOAD,
        patterns: Sequence[str] | None = None,
        execute: bool = False,
    ) -> JobResult: ...

    def log(
        self,
        job_id: str,
        *,
        tail: int | None = None,
        follow: bool = False,
        execute: bool = False,
    ) -> JobResult:
        return self.logs(job_id, tail=tail, follow=follow, execute=execute)

    def get_logs(
        self,
        job_id: str,
        *,
        tail: int | None = None,
        follow: bool = False,
        execute: bool = False,
    ) -> JobResult:
        return self.logs(job_id, tail=tail, follow=follow, execute=execute)

    def artifact_sync(
        self,
        job_id: str,
        local_path: str | Path,
        *,
        direction: ArtifactDirection | str = ArtifactDirection.DOWNLOAD,
        patterns: Sequence[str] | None = None,
        execute: bool = False,
    ) -> JobResult:
        return self.sync_artifacts(
            job_id,
            local_path,
            direction=direction,
            patterns=patterns,
            execute=execute,
        )

    def plan(self, spec: JobSpec) -> JobResult:
        return self.submit(spec, execute=False)

    def execute(self, spec: JobSpec) -> JobResult:
        return self.submit(spec, execute=True)
