from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from paperforge.policy import Action

from ._artifacts import artifact_patterns
from .base import ComputeBackend
from .contracts import ArtifactDirection, JobResult, JobSpec, JobStatus

_RUNTIME_PATTERN = re.compile(r"^[A-Za-z0-9_./-]+$")


@dataclass(frozen=True)
class DockerConfig:
    image: str
    runtime: str = "docker"
    container_workdir: str = "/workspace"
    workspace_mount: str | Path | None = None
    remove_on_exit: bool = False
    extra_args: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.image or "\x00" in self.image:
            raise ValueError("docker image must be non-empty")
        if not _RUNTIME_PATTERN.fullmatch(self.runtime):
            raise ValueError("docker runtime contains unsafe characters")
        workdir = PurePosixPath(self.container_workdir)
        if not workdir.is_absolute() or ".." in workdir.parts:
            raise ValueError("container_workdir must be an absolute container path")
        if any("\x00" in arg for arg in self.extra_args):
            raise ValueError("docker extra_args contain a NUL byte")
        object.__setattr__(self, "extra_args", tuple(self.extra_args))


class DockerBackend(ComputeBackend):
    name = "docker"
    policy_action = Action.CONTAINER_EXECUTE

    def __init__(
        self,
        config: DockerConfig | None = None,
        *,
        image: str | None = None,
        **kwargs: Any,
    ) -> None:
        if config is not None and image is not None:
            raise TypeError("pass either DockerConfig or image, not both")
        self.config = config or DockerConfig(image=image or "")
        super().__init__(**kwargs)

    @staticmethod
    def _container_name(job_id: str) -> str:
        normalized = re.sub(r"[^a-z0-9_.-]+", "-", job_id.lower()).strip("-.")
        digest = hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:10]
        prefix = normalized[: 63 - len(digest) - 1].rstrip("-.")
        return f"{prefix or 'paperforge'}-{digest}"

    def _container_workdir(self, spec: JobSpec) -> str:
        raw = spec.backend_options.get("container_workdir", self.config.container_workdir)
        workdir = PurePosixPath(str(raw))
        if not workdir.is_absolute() or ".." in workdir.parts:
            raise ValueError("container_workdir must be absolute and traversal-free")
        return workdir.as_posix()

    def _submit_argv(self, spec: JobSpec, job_id: str) -> tuple[str, ...]:
        container_name = self._container_name(job_id)
        argv: list[str] = [
            self.config.runtime,
            "run",
            "--detach",
            "--name",
            container_name,
            "--workdir",
            self._container_workdir(spec),
        ]
        if self.config.remove_on_exit:
            argv.append("--rm")
        if self.config.workspace_mount is not None:
            host = Path(self.config.workspace_mount).expanduser().resolve()
            argv.extend(
                [
                    "--volume",
                    f"{host}:{self._container_workdir(spec)}",
                ]
            )
        for key, value in sorted(spec.env.items()):
            argv.extend(["--env", f"{key}={value}"])
        argv.extend(["--cpus", str(spec.resources.cpus)])
        if spec.resources.memory_mb is not None:
            argv.extend(["--memory", f"{spec.resources.memory_mb}m"])
        if spec.resources.gpus:
            argv.extend(["--gpus", str(spec.resources.gpus)])
        argv.extend(self.config.extra_args)
        argv.append(str(spec.backend_options.get("image", self.config.image)))
        argv.extend(spec.command)
        return tuple(argv)

    def submit(self, spec: JobSpec, *, execute: bool | None = None) -> JobResult:
        job_id = self._job_id(spec)
        argv = self._submit_argv(spec, job_id)
        plan = self._plan(
            job_id=job_id,
            action="submit",
            argv=argv,
            description=f"create detached container for {job_id}",
            environment_keys=tuple(spec.env),
            metadata={"container_name": self._container_name(job_id)},
            sensitive_values=self._sensitive_environment_values(spec.env),
        )
        self._remember(job_id, spec=spec, result=plan)
        if not self._should_execute(spec, execute):
            return plan
        self._reject_sensitive_remote_environment(spec)
        outcome = self._run(
            spec,
            argv,
            timeout=spec.resources.timeout_seconds,
        )
        status = JobStatus.SUBMITTED if outcome.return_code == 0 else JobStatus.FAILED
        metadata = {
            "container_name": self._container_name(job_id),
            "container_id": outcome.stdout.strip() or None,
        }
        result = JobResult(
            job_id=job_id,
            backend=self.name,
            status=status,
            executed=True,
            plan=plan.plan,
            return_code=outcome.return_code,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            message="container submitted"
            if outcome.return_code == 0
            else "container submit failed",
            metadata=metadata,
            created_at=plan.created_at,
        )
        self._remember(job_id, result=result)
        return result

    def status(self, job_id: str, *, execute: bool = False) -> JobResult:
        spec = self._known_spec(job_id)
        name = self._container_name(job_id)
        argv = (
            self.config.runtime,
            "inspect",
            "--format",
            "{{.State.Status}}|{{.State.ExitCode}}",
            name,
        )
        plan = self._plan(
            job_id=job_id,
            action="status",
            argv=argv,
            description=f"inspect container {name}",
        )
        if not execute:
            return plan
        outcome = self._run(spec, argv)
        raw_status, _, raw_code = outcome.stdout.strip().partition("|")
        mapping = {
            "created": JobStatus.SUBMITTED,
            "running": JobStatus.RUNNING,
            "paused": JobStatus.SUSPENDED,
            "restarting": JobStatus.RUNNING,
            "exited": JobStatus.SUCCEEDED if raw_code == "0" else JobStatus.FAILED,
            "dead": JobStatus.FAILED,
            "removing": JobStatus.CANCELLED,
        }
        status = (
            mapping.get(raw_status, JobStatus.UNKNOWN)
            if outcome.return_code == 0
            else JobStatus.UNKNOWN
        )
        return_code = int(raw_code) if raw_code.lstrip("-").isdigit() else None
        previous = self._results.get(job_id)
        result = JobResult(
            job_id=job_id,
            backend=self.name,
            status=status,
            executed=True,
            plan=plan.plan,
            return_code=return_code,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            message=f"container state: {raw_status or 'unknown'}",
            metadata=previous.metadata if previous else {"container_name": name},
            created_at=previous.created_at if previous else plan.created_at,
        )
        self._remember(job_id, result=result)
        return result

    def cancel(self, job_id: str, *, execute: bool = False) -> JobResult:
        spec = self._known_spec(job_id)
        name = self._container_name(job_id)
        argv = (self.config.runtime, "stop", name)
        plan = self._plan(
            job_id=job_id,
            action="cancel",
            argv=argv,
            description=f"stop container {name}",
        )
        if not execute:
            return plan
        outcome = self._run(spec, argv)
        status = JobStatus.CANCELLED if outcome.return_code == 0 else JobStatus.FAILED
        result = JobResult(
            job_id=job_id,
            backend=self.name,
            status=status,
            executed=True,
            plan=plan.plan,
            return_code=outcome.return_code,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            message="container stopped" if outcome.return_code == 0 else "container stop failed",
            metadata={"container_name": name},
        )
        self._remember(job_id, result=result)
        return result

    def resume(self, job_id: str, *, execute: bool = False) -> JobResult:
        spec = self._known_spec(job_id)
        name = self._container_name(job_id)
        argv = (self.config.runtime, "start", name)
        plan = self._plan(
            job_id=job_id,
            action="resume",
            argv=argv,
            description=f"restart container {name}",
        )
        if not execute:
            return plan
        outcome = self._run(spec, argv)
        status = JobStatus.SUBMITTED if outcome.return_code == 0 else JobStatus.FAILED
        result = JobResult(
            job_id=job_id,
            backend=self.name,
            status=status,
            executed=True,
            plan=plan.plan,
            return_code=outcome.return_code,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            message="container restarted"
            if outcome.return_code == 0
            else "container restart failed",
            metadata={"container_name": name},
        )
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
        if follow and execute:
            raise ValueError("follow=True is not supported for finite API responses")
        spec = self._known_spec(job_id)
        argv: list[str] = [self.config.runtime, "logs"]
        if tail is not None:
            argv.extend(["--tail", str(tail)])
        if follow:
            argv.append("--follow")
        argv.append(self._container_name(job_id))
        plan = self._plan(
            job_id=job_id,
            action="logs",
            argv=argv,
            description=f"read container logs for {job_id}",
        )
        if not execute:
            return plan
        outcome = self._run(spec, argv)
        previous = self._results.get(job_id)
        return JobResult(
            job_id=job_id,
            backend=self.name,
            status=previous.status if previous else JobStatus.UNKNOWN,
            executed=True,
            plan=plan.plan,
            return_code=outcome.return_code,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            message="container log snapshot",
            metadata={"container_name": self._container_name(job_id)},
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
        local_root = Path(local_path).expanduser().resolve()
        container_root = PurePosixPath(self._container_workdir(spec))
        name = self._container_name(job_id)
        commands: list[tuple[str, ...]] = []
        for pattern in selected:
            remote = f"{name}:{(container_root / pattern).as_posix()}"
            if direction is ArtifactDirection.DOWNLOAD:
                argv = (
                    self.config.runtime,
                    "cp",
                    remote,
                    str(local_root / pattern),
                )
            else:
                argv = (
                    self.config.runtime,
                    "cp",
                    str(local_root / pattern),
                    remote,
                )
            commands.append(argv)
        plan = self._plan(
            job_id=job_id,
            action="artifact-sync",
            argv=commands[0],
            description=(
                f"{direction.value} {len(commands)} container artifact path(s) for {job_id}"
            ),
            metadata={"commands": [list(command) for command in commands]},
        )
        if not execute:
            return plan
        if direction is ArtifactDirection.DOWNLOAD:
            local_root.mkdir(parents=True, exist_ok=True)
        stdout: list[str] = []
        stderr: list[str] = []
        return_code = 0
        for command in commands:
            if direction is ArtifactDirection.DOWNLOAD:
                Path(command[-1]).parent.mkdir(parents=True, exist_ok=True)
            outcome = self._run(spec, command)
            stdout.append(outcome.stdout)
            stderr.append(outcome.stderr)
            if outcome.return_code != 0:
                return_code = outcome.return_code
                break
        return JobResult(
            job_id=job_id,
            backend=self.name,
            status=JobStatus.SUCCEEDED if return_code == 0 else JobStatus.FAILED,
            executed=True,
            plan=plan.plan,
            return_code=return_code,
            stdout="".join(stdout),
            stderr="".join(stderr),
            message=(
                "container artifact sync completed"
                if return_code == 0
                else "container artifact sync failed"
            ),
            metadata={"paths": list(selected), "direction": direction.value},
        )


DockerComputeBackend = DockerBackend
