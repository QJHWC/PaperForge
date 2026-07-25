from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from engine.secret_redaction import redact_secrets
from paperforge.policy import Action

from ._artifacts import artifact_patterns, copy_local_artifacts
from .base import ComputeBackend, JobStateError
from .contracts import (
    ArtifactDirection,
    JobResult,
    JobSpec,
    JobStatus,
    utc_now,
)


class LocalBackend(ComputeBackend):
    name = "local"
    policy_action = Action.LOCAL_EXECUTE

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._processes: dict[str, subprocess.Popen[bytes]] = {}
        self._log_paths: dict[str, Path] = {}

    def submit(self, spec: JobSpec, *, execute: bool | None = None) -> JobResult:
        job_id = self._job_id(spec)
        plan = self._plan(
            job_id=job_id,
            action="submit",
            argv=spec.command,
            description=f"start local job {job_id}",
            cwd=spec.workdir,
            environment_keys=tuple(spec.env),
            sensitive_values=self._sensitive_environment_values(spec.env),
        )
        self._remember(job_id, spec=spec, result=plan)
        if not self._should_execute(spec, execute):
            return plan

        self.policy.validate_command(spec.command, self.policy_action)
        workdir = Path(spec.workdir).expanduser().resolve()
        if not workdir.is_dir():
            raise FileNotFoundError(f"local job workdir does not exist: {workdir}")
        job_dir = self.state_dir / self.name / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        log_path = job_dir / "job.log"
        environment = os.environ.copy()
        environment.update(spec.env)

        with log_path.open("wb") as log_handle:
            process = subprocess.Popen(
                list(spec.command),
                cwd=workdir,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

        result = JobResult(
            job_id=job_id,
            backend=self.name,
            status=JobStatus.RUNNING,
            executed=True,
            plan=plan.plan,
            message="local process started",
            metadata={
                "pid": process.pid,
                "log_path": str(log_path),
                "workdir": str(workdir),
                "attempt": 1,
            },
            created_at=plan.created_at,
        )
        with self._lock:
            self._processes[job_id] = process
            self._log_paths[job_id] = log_path
        self._remember(job_id, result=result)
        return result

    def status(self, job_id: str, *, execute: bool = False) -> JobResult:
        plan = self._plan(
            job_id=job_id,
            action="status",
            argv=("paperforge-local-status", job_id),
            description=f"inspect local job {job_id}",
        )
        if not execute:
            return plan

        previous = self._known_result(job_id)
        process = self._processes.get(job_id)
        if process is None:
            return previous
        return_code = process.poll()
        if return_code is None:
            status = JobStatus.RUNNING
            message = "local process is running"
        elif return_code == 0:
            status = JobStatus.SUCCEEDED
            message = "local process completed"
        else:
            status = JobStatus.FAILED
            message = f"local process exited with code {return_code}"
        result = JobResult(
            job_id=job_id,
            backend=self.name,
            status=status,
            executed=True,
            plan=previous.plan,
            return_code=return_code,
            message=message,
            metadata=previous.metadata,
            created_at=previous.created_at,
            updated_at=utc_now(),
        )
        self._remember(job_id, result=result)
        return result

    def cancel(self, job_id: str, *, execute: bool = False) -> JobResult:
        plan = self._plan(
            job_id=job_id,
            action="cancel",
            argv=("paperforge-local-cancel", job_id),
            description=f"terminate local job {job_id}",
        )
        if not execute:
            return plan

        spec = self._known_spec(job_id)
        self.policy.validate_command(spec.command, self.policy_action)
        previous = self.status(job_id, execute=True)
        process = self._processes.get(job_id)
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except (AttributeError, ProcessLookupError):
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (AttributeError, ProcessLookupError):
                    process.kill()
                process.wait(timeout=5)
        result = JobResult(
            job_id=job_id,
            backend=self.name,
            status=JobStatus.CANCELLED,
            executed=True,
            plan=plan.plan,
            return_code=process.returncode if process is not None else previous.return_code,
            message="local job cancelled",
            metadata=previous.metadata,
            created_at=previous.created_at,
        )
        self._remember(job_id, result=result)
        return result

    def resume(self, job_id: str, *, execute: bool = False) -> JobResult:
        spec = self._known_spec(job_id)
        plan = self._plan(
            job_id=job_id,
            action="resume",
            argv=spec.command,
            description=f"restart local job {job_id}",
            cwd=spec.workdir,
            environment_keys=tuple(spec.env),
            sensitive_values=self._sensitive_environment_values(spec.env),
        )
        if not execute:
            return plan

        current = self.status(job_id, execute=True)
        if current.status in {JobStatus.RUNNING, JobStatus.SUBMITTED, JobStatus.QUEUED}:
            raise JobStateError(f"cannot resume active local job {job_id}")
        self.policy.validate_command(spec.command, self.policy_action)
        workdir = Path(spec.workdir).expanduser().resolve()
        log_path = self.state_dir / self.name / job_id / "job.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        environment = os.environ.copy()
        environment.update(spec.env)
        with log_path.open("ab") as log_handle:
            log_handle.write(b"\n--- resumed ---\n")
            process = subprocess.Popen(
                list(spec.command),
                cwd=workdir,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        metadata = dict(current.metadata)
        metadata.update(
            {
                "pid": process.pid,
                "attempt": int(metadata.get("attempt", 1)) + 1,
                "log_path": str(log_path),
            }
        )
        result = JobResult(
            job_id=job_id,
            backend=self.name,
            status=JobStatus.RUNNING,
            executed=True,
            plan=plan.plan,
            message="local job resumed",
            metadata=metadata,
            created_at=current.created_at,
        )
        with self._lock:
            self._processes[job_id] = process
            self._log_paths[job_id] = log_path
        self._remember(job_id, result=result)
        return result

    def logs(
        self,
        job_id: str,
        *,
        tail: int | None = None,
        follow: bool = False,
        execute: bool = False,
    ) -> JobResult:
        if tail is not None and tail < 1:
            raise ValueError("tail must be positive")
        argv: tuple[str, ...] = ("paperforge-local-logs", job_id)
        if tail is not None:
            argv += ("--tail", str(tail))
        if follow:
            argv += ("--follow",)
        plan = self._plan(
            job_id=job_id,
            action="logs",
            argv=argv,
            description=f"read local job log for {job_id}",
        )
        if not execute:
            return plan
        if follow:
            raise ValueError("follow=True is not supported for finite API responses")
        previous = self._known_result(job_id)
        log_path = self._log_paths.get(job_id)
        if log_path is None or not log_path.exists():
            content = ""
        else:
            content = log_path.read_text(encoding="utf-8", errors="replace")
        if tail is not None:
            content = "".join(content.splitlines(keepends=True)[-tail:])
        spec = self._known_spec(job_id)
        content = redact_secrets(
            content,
            secret_values=self._sensitive_environment_values(spec.env),
        )
        return JobResult(
            job_id=job_id,
            backend=self.name,
            status=previous.status,
            executed=True,
            plan=plan.plan,
            return_code=previous.return_code,
            stdout=content,
            message="local job log snapshot",
            metadata=previous.metadata,
            created_at=previous.created_at,
        )

    def sync_artifacts(
        self,
        job_id: str,
        local_path: str | Path,
        *,
        direction: ArtifactDirection | str = ArtifactDirection.DOWNLOAD,
        patterns: Sequence[str] | None = None,
        execute: bool = False,
    ) -> JobResult:
        direction = ArtifactDirection(direction)
        spec = self._known_spec(job_id)
        selected = artifact_patterns(
            spec.outputs,
            tuple(str(path) for path in patterns) if patterns is not None else None,
        )
        workdir = Path(spec.workdir).expanduser().resolve()
        local_root = Path(local_path).expanduser().resolve()
        if direction is ArtifactDirection.DOWNLOAD:
            source_root, destination_root = workdir, local_root
        else:
            source_root, destination_root = local_root, workdir
        plan = self._plan(
            job_id=job_id,
            action="artifact-sync",
            argv=(
                "paperforge-local-copy",
                direction.value,
                str(source_root),
                str(destination_root),
                *selected,
            ),
            description=f"{direction.value} artifacts for local job {job_id}",
        )
        if not execute:
            return plan

        self.policy.require(self.policy_action, detail=f"artifact sync {job_id}")
        try:
            artifacts = copy_local_artifacts(
                source_root=source_root,
                destination_root=destination_root,
                patterns=selected,
            )
        except (OSError, ValueError) as exc:
            return JobResult(
                job_id=job_id,
                backend=self.name,
                status=JobStatus.FAILED,
                executed=True,
                plan=plan.plan,
                return_code=1,
                stderr=str(exc),
                message="artifact sync failed",
            )
        return JobResult(
            job_id=job_id,
            backend=self.name,
            status=JobStatus.SUCCEEDED,
            executed=True,
            plan=plan.plan,
            return_code=0,
            artifacts=artifacts,
            message=f"synced {len(artifacts)} artifact files",
        )


LocalComputeBackend = LocalBackend
