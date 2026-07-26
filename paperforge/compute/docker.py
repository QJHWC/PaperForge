from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from paperforge.path_safety import (
    atomic_write_text,
    is_link_or_reparse_point,
    reject_symlink_components,
    safe_mkdir,
)
from paperforge.policy import Action

from ._artifacts import artifact_patterns, copy_local_artifacts
from .base import ComputeBackend, JobStateError, SubprocessCommandRunner
from .contracts import ArtifactDirection, JobResult, JobSpec, JobStatus

_RUNTIME_PATTERN = re.compile(r"^[A-Za-z0-9_./-]+$")
_IMAGE_DIGEST = re.compile(r"^.+@sha256:[0-9a-fA-F]{64}$")
_OUTPUT_MOUNT_ROOT = PurePosixPath("/paperforge-outputs")
_FORBIDDEN_EXTRA_ARGS = frozenset(
    {
        "--cap-add",
        "--device",
        "--env",
        "--env-file",
        "--ipc",
        "--mount",
        "--network",
        "--pid",
        "--privileged",
        "--security-opt",
        "--userns",
        "--volume",
        "-e",
        "-v",
    }
)


@dataclass(frozen=True)
class DockerConfig:
    image: str
    runtime: str = "docker"
    container_workdir: str = "/workspace"
    workspace_mount: str | Path | None = None
    container_user: str | None = None
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
        for arg in self.extra_args:
            option = arg.split("=", 1)[0]
            if option in _FORBIDDEN_EXTRA_ARGS:
                raise ValueError(
                    f"docker extra_args cannot override security: {option}"
                )
        if self.container_user is not None and not re.fullmatch(
            r"[1-9][0-9]{0,9}:[1-9][0-9]{0,9}",
            self.container_user,
        ):
            raise ValueError(
                "container_user must be a non-root numeric uid:gid"
            )
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
    def _container_name(job_id: str, attempt: int = 1) -> str:
        normalized = re.sub(r"[^a-z0-9_.-]+", "-", job_id.lower()).strip("-.")
        digest = hashlib.sha256(f"{job_id}:{attempt}".encode()).hexdigest()[:10]
        prefix = normalized[: 63 - len(digest) - 1].rstrip("-.")
        return f"{prefix or 'paperforge'}-{digest}"

    def _container_workdir(self, spec: JobSpec) -> str:
        raw = spec.backend_options.get("container_workdir", self.config.container_workdir)
        workdir = PurePosixPath(str(raw))
        if not workdir.is_absolute() or ".." in workdir.parts:
            raise ValueError("container_workdir must be absolute and traversal-free")
        return workdir.as_posix()

    def _artifact_root(self, job_id: str, attempt: int) -> Path:
        return self._job_state_path(
            job_id,
            f"attempts/{attempt}/artifacts",
        )

    def _workspace_root(self, job_id: str, attempt: int) -> Path:
        return self._job_state_path(
            job_id,
            f"attempts/{attempt}/workspace",
        )

    def _prepare_attempt_outputs(
        self,
        spec: JobSpec,
        job_id: str,
        attempt: int,
    ) -> tuple[Path, Path]:
        job_dir = self._job_state_dir(job_id)
        attempt_root = job_dir / "attempts" / str(attempt)
        reject_symlink_components(attempt_root, anchor=job_dir)
        if attempt_root.exists():
            if is_link_or_reparse_point(attempt_root) or not attempt_root.is_dir():
                raise JobStateError("Docker attempt path is unsafe")
            shutil.rmtree(attempt_root)
        artifact_root = safe_mkdir(
            attempt_root / "artifacts",
            anchor=job_dir,
        )
        source_root = Path(self.config.workspace_mount or "").expanduser().resolve(
            strict=True
        )
        if not source_root.is_dir() or is_link_or_reparse_point(source_root):
            raise JobStateError("Docker workspace snapshot must be a regular directory")
        for candidate in source_root.rglob("*"):
            if is_link_or_reparse_point(candidate):
                raise JobStateError(
                    "Docker workspace snapshot contains a symbolic link or reparse point"
                )
        workspace_root = attempt_root / "workspace"
        shutil.copytree(source_root, workspace_root)
        for relative_output in spec.outputs:
            if any(character in str(relative_output) for character in "*?[]"):
                raise JobStateError(
                    "executable Docker outputs must be explicit file paths"
                )
            output = artifact_root / relative_output
            safe_mkdir(output.parent, anchor=artifact_root)
            reject_symlink_components(output, anchor=artifact_root)
            with output.open("wb"):
                pass
            workspace_output = workspace_root / relative_output
            reject_symlink_components(workspace_output.parent, anchor=workspace_root)
            safe_mkdir(workspace_output.parent, anchor=workspace_root)
            if workspace_output.exists() or workspace_output.is_symlink():
                if not workspace_output.is_file() and not workspace_output.is_symlink():
                    raise JobStateError("declared Docker output collides with a directory")
                workspace_output.unlink()
            workspace_output.symlink_to(
                (_OUTPUT_MOUNT_ROOT / str(spec.outputs.index(relative_output))).as_posix()
            )
        return artifact_root, workspace_root

    def _submit_argv(
        self,
        spec: JobSpec,
        job_id: str,
        *,
        attempt: int = 1,
        artifact_root: Path | None = None,
        workspace_root: Path | None = None,
    ) -> tuple[str, ...]:
        container_name = self._container_name(job_id, attempt)
        image = str(spec.backend_options.get("image", self.config.image))
        if spec.execute and self.config.remove_on_exit:
            raise JobStateError(
                "executable Docker jobs cannot use remove_on_exit; "
                "the container is retained for durable recovery"
            )
        if spec.execute and not _IMAGE_DIGEST.fullmatch(image):
            raise JobStateError(
                "executable Docker jobs require an image pinned by sha256 digest"
            )
        if spec.execute and self.config.workspace_mount is None:
            raise JobStateError(
                "executable Docker jobs require an immutable workspace snapshot"
            )
        if spec.execute and self.config.extra_args:
            raise JobStateError(
                "executable Docker jobs do not accept unbound extra_args"
            )
        argv: list[str] = [
            self.config.runtime,
            "run",
            "--detach",
            "--name",
            container_name,
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            self.config.container_user
            or (
                f"{os.getuid()}:{os.getgid()}"
                if hasattr(os, "getuid")
                else "65532:65532"
            ),
            "--pids-limit",
            "512",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=256m",
            "--workdir",
            self._container_workdir(spec),
        ]
        if self.config.remove_on_exit:
            argv.append("--rm")
        if self.config.workspace_mount is not None:
            host = (
                workspace_root
                if spec.execute and workspace_root is not None
                else (
                    self._workspace_root(job_id, attempt)
                    if spec.execute
                    else Path(self.config.workspace_mount).expanduser().resolve()
                )
            )
            if (host.is_symlink() or not host.is_dir()) and not spec.execute:
                raise JobStateError(
                    "Docker workspace snapshot must be a regular directory"
                )
            argv.extend(
                [
                    "--volume",
                    f"{host}:{self._container_workdir(spec)}:ro",
                ]
            )
            output_root = artifact_root or self._artifact_root(job_id, attempt)
            if spec.execute and spec.outputs:
                argv.extend(
                    [
                        "--tmpfs",
                        f"{_OUTPUT_MOUNT_ROOT}:rw,noexec,nosuid,nodev,size=64m",
                    ]
                )
            for index, relative_output in enumerate(spec.outputs):
                if any(
                    character in str(relative_output)
                    for character in "*?[]"
                ):
                    raise JobStateError(
                        "executable Docker outputs must be explicit file paths"
                    )
                host_output = output_root / relative_output
                container_output = (
                    (_OUTPUT_MOUNT_ROOT / str(index)).as_posix()
                    if spec.execute
                    else (
                        PurePosixPath(self._container_workdir(spec))
                        / relative_output
                    ).as_posix()
                )
                argv.extend(
                    [
                        "--volume",
                        f"{host_output}:{container_output}:rw",
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
        argv.append(image)
        argv.extend(spec.command)
        return tuple(argv)

    @staticmethod
    def _canonical_sha256(payload: dict[str, Any]) -> str:
        encoded = json.dumps(
            payload,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _launch_deadline_watchdog(
        self,
        spec: JobSpec,
        *,
        job_id: str,
        attempt: int,
        container_name: str,
        container_id: str,
        deadline_epoch: float,
    ) -> int | None:
        if not isinstance(self.runner, SubprocessCommandRunner):
            return None
        runtime = Path(
            shutil.which(self.config.runtime) or self.config.runtime
        ).resolve(strict=True)
        attempt_root = self._artifact_root(job_id, attempt).parent
        marker = attempt_root / "timeout.json"
        identity = self._canonical_sha256(
            {
                "job_id": job_id,
                "attempt": attempt,
                "container_name": container_name,
                "container_id": container_id,
                "job_fingerprint": spec.fingerprint,
            }
        )
        payload: dict[str, Any] = {
            "schema": "paperforge.compute-watchdog/v1",
            "runtime": str(runtime),
            "container_name": container_name,
            "container_id": container_id,
            "deadline_epoch": deadline_epoch,
            "timeout_marker": str(marker),
            "identity_sha256": identity,
        }
        payload["config_sha256"] = self._canonical_sha256(payload)
        config_path = attempt_root / "watchdog.json"
        atomic_write_text(
            attempt_root,
            config_path.name,
            json.dumps(payload, sort_keys=True) + "\n",
        )
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        if os.name == "nt" and os.environ.get("SYSTEMROOT"):
            environment["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
        options: dict[str, Any] = {
            "env": environment,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "cwd": attempt_root,
        }
        if os.name == "nt":
            options["creationflags"] = getattr(
                subprocess,
                "CREATE_NEW_PROCESS_GROUP",
                0x00000200,
            ) | getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        else:
            options["start_new_session"] = True
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "paperforge.compute._deadline_watchdog",
                str(config_path),
            ],
            **options,
        )
        return process.pid

    def _remove_exact_container(
        self,
        spec: JobSpec,
        *,
        container_name: str,
        container_id: str,
    ) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", container_id):
            return
        identity = self._run(
            spec,
            (
                self.config.runtime,
                "inspect",
                "--format",
                "{{.Id}}",
                container_name,
            ),
            timeout=60,
        )
        if identity.return_code == 0 and identity.stdout.strip() == container_id:
            self._run(
                spec,
                (self.config.runtime, "rm", "--force", container_name),
                timeout=60,
            )

    def submit(self, spec: JobSpec, *, execute: bool | None = None) -> JobResult:
        job_id = self._job_id(spec)
        attempt = 1
        artifact_root = self._artifact_root(job_id, attempt)
        argv = self._submit_argv(
            spec,
            job_id,
            attempt=attempt,
            artifact_root=artifact_root,
        )
        container_name = self._container_name(job_id, attempt)
        plan = self._plan(
            job_id=job_id,
            action="submit",
            argv=argv,
            description=f"create detached container for {job_id}",
            environment_keys=tuple(spec.env),
            metadata={
                "container_name": container_name,
                "attempt": attempt,
                "artifact_root": str(artifact_root),
            },
            sensitive_values=self._sensitive_environment_values(spec.env),
        )
        self._remember(job_id, spec=spec, result=plan)
        if not self._should_execute(spec, execute):
            return plan
        self._reject_sensitive_remote_environment(spec)
        if spec.execute:
            artifact_root, workspace_root = self._prepare_attempt_outputs(
                spec,
                job_id,
                attempt,
            )
            argv = self._submit_argv(
                spec,
                job_id,
                attempt=attempt,
                artifact_root=artifact_root,
                workspace_root=workspace_root,
            )
        self._persist_submission_intent(spec, plan)
        outcome = self._run(
            spec,
            argv,
            timeout=60,
        )
        raw_container_id = outcome.stdout.strip()
        container_id = (
            raw_container_id
            if re.fullmatch(r"[0-9a-f]{64}", raw_container_id)
            else None
        )
        status = (
            JobStatus.SUBMITTED
            if outcome.return_code == 0 and container_id is not None
            else JobStatus.FAILED
        )
        deadline_epoch = (
            time.time() + spec.resources.timeout_seconds
            if spec.resources.timeout_seconds is not None
            and status is JobStatus.SUBMITTED
            else None
        )
        watchdog_pid = None
        if deadline_epoch is not None and container_id is not None:
            try:
                watchdog_pid = self._launch_deadline_watchdog(
                    spec,
                    job_id=job_id,
                    attempt=attempt,
                    container_name=container_name,
                    container_id=container_id,
                    deadline_epoch=deadline_epoch,
                )
            except Exception:
                self._remove_exact_container(
                    spec,
                    container_name=container_name,
                    container_id=container_id,
                )
                raise
        metadata = {
            "container_name": container_name,
            "container_id": container_id,
            "attempt": attempt,
            "artifact_root": str(artifact_root),
            "deadline_epoch": deadline_epoch,
            "watchdog_pid": watchdog_pid,
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
        try:
            self._remember(job_id, result=result)
        except Exception:
            if status is JobStatus.SUBMITTED:
                self._remove_exact_container(
                    spec,
                    container_name=container_name,
                    container_id=container_id or "",
                )
            raise
        return result

    def status(self, job_id: str, *, execute: bool = False) -> JobResult:
        spec = self._known_spec(job_id)
        previous = self._known_result(job_id)
        metadata = dict(previous.metadata)
        attempt = int(metadata.get("attempt") or 1)
        name = str(metadata.get("container_name") or self._container_name(job_id, attempt))
        expected_id = str(metadata.get("container_id") or "")
        argv = (
            self.config.runtime,
            "inspect",
            "--format",
            "{{.Id}}|{{.State.Status}}|{{.State.ExitCode}}",
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
        raw_id, _, remainder = outcome.stdout.strip().partition("|")
        raw_status, _, raw_code = remainder.partition("|")
        marker = self._artifact_root(job_id, attempt).parent / "timeout.json"
        # Once this backend has enforced a deadline, keep the terminal result
        # stable even while Docker briefly reports the forced container as
        # "removing" with its raw SIGKILL exit code.
        timed_out = metadata.get("timed_out") is True
        if marker.is_file() and not is_link_or_reparse_point(marker):
            try:
                marker_payload = json.loads(marker.read_text(encoding="utf-8"))
                timed_out = timed_out or (
                    marker_payload.get("status") == "TIMED_OUT"
                    and marker_payload.get("container_id") == expected_id
                    and marker_payload.get("container_name") == name
                )
            except (OSError, json.JSONDecodeError):
                timed_out = False
        if timed_out:
            status = JobStatus.FAILED
            return_code = 124
            raw_status = "timed_out"
            metadata.pop("cleanup_pending", None)
            metadata["timed_out"] = True
        elif outcome.return_code == 0 and expected_id and raw_id != expected_id:
            status = JobStatus.UNKNOWN
            return_code = None
            raw_status = "identity_mismatch"
        else:
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
        deadline = metadata.get("deadline_epoch")
        deadline_value = (
            float(deadline)
            if isinstance(deadline, int | float) and not isinstance(deadline, bool)
            else None
        )
        deadline_now = (
            time.time()
            if status is not JobStatus.FAILED
            or metadata.get("cleanup_pending") is True
            else None
        )
        deadline_elapsed = (
            deadline_value is not None
            and deadline_now is not None
            and deadline_now >= deadline_value
        )
        watchdog_pid = metadata.get("watchdog_pid")
        watchdog_active = (
            isinstance(watchdog_pid, int)
            and not isinstance(watchdog_pid, bool)
            and watchdog_pid > 0
        )
        within_marker_grace = (
            watchdog_active
            and deadline_value is not None
            and deadline_now is not None
            and deadline_now <= deadline_value + 5.0
        )
        observed_raw_status = raw_status
        if (
            metadata.get("cleanup_pending") is True
            and deadline_elapsed
            and outcome.return_code == 0
            and bool(expected_id)
            and raw_id == expected_id
            and raw_status != "removing"
        ):
            # A deadline cleanup that has started can never later become a
            # successful job merely because the process exited on its own.
            status = JobStatus.RUNNING
            return_code = None
        if (
            raw_status == "removing"
            and deadline_elapsed
            and bool(expected_id)
            and raw_id == expected_id
        ):
            status = JobStatus.RUNNING
            return_code = None
            raw_status = "timeout_cleanup_pending"
            metadata["cleanup_pending"] = True
        elif (
            deadline_elapsed
            and outcome.return_code != 0
            and watchdog_active
        ):
            status = JobStatus.RUNNING if within_marker_grace else JobStatus.UNKNOWN
            return_code = None
            raw_status = (
                "timeout_cleanup_pending"
                if within_marker_grace
                else "timeout_cleanup_unconfirmed"
            )
            metadata["cleanup_pending"] = True
        if (
            status is JobStatus.RUNNING
            and raw_status != "timeout_cleanup_pending"
            and deadline_elapsed
            and bool(expected_id)
            and raw_id == expected_id
        ):
            stopped = self._run(
                spec,
                (self.config.runtime, "rm", "--force", name),
                timeout=60,
            )
            verified = self._run(
                spec,
                (
                    self.config.runtime,
                    "inspect",
                    "--format",
                    "{{.Id}}",
                    name,
                ),
                timeout=30,
            )
            outcome = type(outcome)(
                stopped.return_code,
                outcome.stdout + stopped.stdout + verified.stdout,
                outcome.stderr + stopped.stderr + verified.stderr,
            )
            if stopped.return_code == 0 and verified.return_code != 0:
                status = JobStatus.FAILED
                return_code = 124
                raw_status = "timed_out"
                metadata.pop("cleanup_pending", None)
                metadata["timed_out"] = True
            else:
                status = (
                    JobStatus.RUNNING
                    if (
                        (
                            verified.return_code == 0
                            and verified.stdout.strip() == expected_id
                            and observed_raw_status
                            in {"created", "running", "paused", "restarting"}
                        )
                        or (verified.return_code != 0 and within_marker_grace)
                    )
                    else JobStatus.UNKNOWN
                )
                return_code = None
                raw_status = "cleanup_pending"
                metadata.pop("timed_out", None)
                metadata["cleanup_pending"] = True
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
            metadata=metadata,
            created_at=previous.created_at,
        )
        self._remember(job_id, result=result)
        return result

    def cancel(self, job_id: str, *, execute: bool = False) -> JobResult:
        spec = self._known_spec(job_id)
        previous = self._known_result(job_id)
        metadata = dict(previous.metadata)
        attempt = int(metadata.get("attempt") or 1)
        name = str(metadata.get("container_name") or self._container_name(job_id, attempt))
        argv = (self.config.runtime, "stop", name)
        plan = self._plan(
            job_id=job_id,
            action="cancel",
            argv=argv,
            description=f"stop container {name}",
        )
        if not execute:
            return plan
        expected_id = str(metadata.get("container_id") or "")
        identity = self._run(
            spec,
            (
                self.config.runtime,
                "inspect",
                "--format",
                "{{.Id}}",
                name,
            ),
        )
        if (
            not re.fullmatch(r"[0-9a-f]{64}", expected_id)
            or identity.return_code != 0
            or identity.stdout.strip() != expected_id
        ):
            return JobResult(
                job_id=job_id,
                backend=self.name,
                status=JobStatus.UNKNOWN,
                executed=True,
                plan=plan.plan,
                return_code=identity.return_code,
                stdout=identity.stdout,
                stderr=identity.stderr,
                message="container identity mismatch; cancellation refused",
                metadata=metadata,
                created_at=previous.created_at,
            )
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
            metadata=metadata,
            created_at=previous.created_at,
        )
        self._remember(job_id, result=result)
        return result

    def resume(self, job_id: str, *, execute: bool = False) -> JobResult:
        spec = self._known_spec(job_id)
        current = self._known_result(job_id)
        attempt = int(current.metadata.get("attempt") or 1) + 1
        name = self._container_name(job_id, attempt)
        artifact_root = self._artifact_root(job_id, attempt)
        argv = self._submit_argv(
            spec,
            job_id,
            attempt=attempt,
            artifact_root=artifact_root,
        )
        plan = self._plan(
            job_id=job_id,
            action="resume",
            argv=argv,
            description=f"start fresh container attempt {attempt} for {name}",
        )
        if not execute:
            return plan
        observed = self.status(job_id, execute=True)
        if observed.metadata.get("cleanup_pending") is True:
            raise JobStateError(
                f"cannot resume Docker job {job_id} while timeout cleanup is pending"
            )
        if observed.status in {
            JobStatus.SUBMITTED,
            JobStatus.QUEUED,
            JobStatus.RUNNING,
            JobStatus.SUSPENDED,
        }:
            raise JobStateError(f"cannot resume active Docker job {job_id}")
        if spec.execute:
            artifact_root, workspace_root = self._prepare_attempt_outputs(
                spec,
                job_id,
                attempt,
            )
            argv = self._submit_argv(
                spec,
                job_id,
                attempt=attempt,
                artifact_root=artifact_root,
                workspace_root=workspace_root,
            )
        outcome = self._run(spec, argv)
        raw_container_id = outcome.stdout.strip()
        container_id = (
            raw_container_id
            if re.fullmatch(r"[0-9a-f]{64}", raw_container_id)
            else None
        )
        status = (
            JobStatus.SUBMITTED
            if outcome.return_code == 0 and container_id is not None
            else JobStatus.FAILED
        )
        deadline_epoch = (
            time.time() + spec.resources.timeout_seconds
            if spec.resources.timeout_seconds is not None
            and status is JobStatus.SUBMITTED
            else None
        )
        watchdog_pid = None
        if deadline_epoch is not None and container_id is not None:
            watchdog_pid = self._launch_deadline_watchdog(
                spec,
                job_id=job_id,
                attempt=attempt,
                container_name=name,
                container_id=container_id,
                deadline_epoch=deadline_epoch,
            )
        result = JobResult(
            job_id=job_id,
            backend=self.name,
            status=status,
            executed=True,
            plan=plan.plan,
            return_code=outcome.return_code,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            message="fresh container attempt submitted"
            if outcome.return_code == 0
                else "container resume failed",
            metadata={
                "container_name": name,
                "container_id": container_id,
                "attempt": attempt,
                "artifact_root": str(artifact_root),
                "deadline_epoch": deadline_epoch,
                "watchdog_pid": watchdog_pid,
            },
            created_at=current.created_at,
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
        previous = self._known_result(job_id)
        attempt = int(previous.metadata.get("attempt") or 1)
        container_name = str(
            previous.metadata.get("container_name")
            or self._container_name(job_id, attempt)
        )
        argv: list[str] = [self.config.runtime, "logs"]
        if tail is not None:
            argv.extend(["--tail", str(tail)])
        if follow:
            argv.append("--follow")
        argv.append(container_name)
        plan = self._plan(
            job_id=job_id,
            action="logs",
            argv=argv,
            description=f"read container logs for {job_id}",
        )
        if not execute:
            return plan
        outcome = self._run(spec, argv)
        return JobResult(
            job_id=job_id,
            backend=self.name,
            status=previous.status,
            executed=True,
            plan=plan.plan,
            return_code=outcome.return_code,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            message="container log snapshot",
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
        local_root = Path(local_path).expanduser().absolute()
        previous = self._known_result(job_id)
        raw_attempt = previous.metadata.get("attempt")
        attempt = (
            int(raw_attempt)
            if isinstance(raw_attempt, int) and not isinstance(raw_attempt, bool)
            else 1
        )
        if (
            direction is ArtifactDirection.DOWNLOAD
            and spec.execute
            and self.config.workspace_mount is not None
        ):
            source_root = self._artifact_root(job_id, attempt)
            plan = self._plan(
                job_id=job_id,
                action="artifact-sync",
                argv=(
                    "paperforge-docker-copy",
                    "download",
                    str(source_root),
                    str(local_root),
                    *selected,
                ),
                description=(
                    f"download {len(selected)} bind-mounted Docker artifact "
                    f"path(s) for {job_id}"
                ),
            )
            if not execute:
                return plan
            self.policy.require(
                self.policy_action,
                detail=f"Docker artifact sync {job_id}",
            )
            try:
                artifacts = copy_local_artifacts(
                    source_root=source_root,
                    destination_root=local_root,
                    patterns=selected,
                    attempt_id=attempt,
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
                    message="container artifact sync failed",
                )
            return JobResult(
                job_id=job_id,
                backend=self.name,
                status=JobStatus.SUCCEEDED,
                executed=True,
                plan=plan.plan,
                return_code=0,
                artifacts=artifacts,
                message=f"synced {len(artifacts)} container artifact files",
                metadata={
                    "paths": list(selected),
                    "direction": direction.value,
                    "attempt": attempt,
                },
            )
        container_root = PurePosixPath(self._container_workdir(spec))
        name = str(
            previous.metadata.get("container_name")
            or self._container_name(job_id, attempt)
        )
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
            metadata={
                "paths": list(selected),
                "direction": direction.value,
                "attempt": attempt,
            },
        )


DockerComputeBackend = DockerBackend
