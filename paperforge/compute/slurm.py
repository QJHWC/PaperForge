from __future__ import annotations

import hashlib
import re
import shlex
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from paperforge.path_safety import (
    is_link_or_reparse_point,
    reject_symlink_components,
    safe_mkdir,
)
from paperforge.policy import Action

from ._artifacts import artifact_patterns, copy_local_artifacts
from .base import ComputeBackend, JobStateError
from .contracts import ArtifactDirection, JobResult, JobSpec, JobStatus

_COMMAND_PATTERN = re.compile(r"^[A-Za-z0-9_./-]+$")
_ALLOWED_EXTRA_SUBMIT_ARGS = (
    "--account=",
    "--constraint=",
    "--qos=",
    "--reservation=",
)


@dataclass(frozen=True)
class SlurmConfig:
    sbatch_executable: str = "sbatch"
    squeue_executable: str = "squeue"
    sacct_executable: str = "sacct"
    scancel_executable: str = "scancel"
    scontrol_executable: str = "scontrol"
    default_partition: str | None = None
    extra_submit_args: tuple[str, ...] = ()
    container_runtime: str | None = None
    container_image: str | Path | None = None

    def __post_init__(self) -> None:
        for label, value in (
            ("sbatch_executable", self.sbatch_executable),
            ("squeue_executable", self.squeue_executable),
            ("sacct_executable", self.sacct_executable),
            ("scancel_executable", self.scancel_executable),
            ("scontrol_executable", self.scontrol_executable),
        ):
            if not _COMMAND_PATTERN.fullmatch(value):
                raise ValueError(f"{label} contains unsafe characters")
        if self.default_partition is not None and not re.fullmatch(
            r"[A-Za-z0-9_.-]+", self.default_partition
        ):
            raise ValueError("default_partition contains unsafe characters")
        invalid_extra_args = [
            value
            for value in self.extra_submit_args
            if "\x00" in value
            or not value.startswith(_ALLOWED_EXTRA_SUBMIT_ARGS)
        ]
        if invalid_extra_args:
            raise ValueError(
                "extra_submit_args contains an option outside the "
                "Slurm allowlist"
            )
        if self.container_runtime is not None and not (
            _COMMAND_PATTERN.fullmatch(self.container_runtime)
        ):
            raise ValueError("container_runtime contains unsafe characters")
        if self.container_image is not None:
            image = Path(self.container_image).expanduser()
            if image.is_symlink():
                raise ValueError(
                    "container_image must not be a symbolic link"
                )
            resolved_image = image.resolve(strict=True)
            if not resolved_image.is_file():
                raise ValueError(
                    "container_image must be a regular file"
                )
            object.__setattr__(self, "container_image", resolved_image)
        object.__setattr__(self, "extra_submit_args", tuple(self.extra_submit_args))


class SlurmBackend(ComputeBackend):
    name = "slurm"
    policy_action = Action.REMOTE_EXECUTE

    def __init__(
        self,
        config: SlurmConfig | None = None,
        **kwargs: Any,
    ) -> None:
        self.config = config or SlurmConfig()
        super().__init__(**kwargs)
        self._slurm_ids: dict[str, str] = {}
        self._log_paths: dict[str, Path] = {}

    def _log_path(self, job_id: str, attempt: int = 1) -> Path:
        return self._job_state_path(
            job_id,
            f"attempts/{attempt}/slurm-%j.log",
        )

    @staticmethod
    def _submission_identity(job_id: str) -> tuple[str, str]:
        marker = hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:24]
        return f"pf_{marker}", f"paperforge:{marker}"

    @staticmethod
    def _duration(seconds: int) -> str:
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def _submit_argv(
        self,
        spec: JobSpec,
        job_id: str,
        attempt: int = 1,
    ) -> tuple[str, ...]:
        log_path = self._log_path(job_id, attempt)
        scheduler_name, scheduler_comment = self._submission_identity(job_id)
        argv: list[str] = [
            self.config.sbatch_executable,
            "--parsable",
            f"--job-name={scheduler_name}",
            f"--comment={scheduler_comment}",
            f"--cpus-per-task={spec.resources.cpus}",
            "--chdir="
            + str(
                self._execution_root(job_id, attempt)
                if spec.execute
                else spec.workdir
            ),
            f"--output={log_path}",
        ]
        if spec.resources.memory_mb is not None:
            argv.append(f"--mem={spec.resources.memory_mb}M")
        if spec.resources.gpus:
            argv.append(f"--gres=gpu:{spec.resources.gpus}")
        if spec.resources.timeout_seconds is not None:
            argv.append(f"--time={self._duration(spec.resources.timeout_seconds)}")
        partition = spec.resources.queue or self.config.default_partition
        if partition:
            argv.append(f"--partition={partition}")
        argv.extend(self.config.extra_submit_args)
        environment = " ".join(
            f"{key}={shlex.quote(value)}" for key, value in sorted(spec.env.items())
        )
        rendered = self._container_command(spec, job_id, attempt)
        if environment and not spec.execute:
            rendered = f"env {environment} {rendered}"
        argv.extend(["--wrap", rendered])
        return tuple(argv)

    def _artifact_root(self, job_id: str, attempt: int = 1) -> Path:
        return self._job_state_path(
            job_id,
            f"attempts/{attempt}/artifacts",
        )

    def _execution_root(self, job_id: str, attempt: int = 1) -> Path:
        return self._job_state_path(
            job_id,
            f"attempts/{attempt}/workspace",
        )

    def _container_command(
        self,
        spec: JobSpec,
        job_id: str,
        attempt: int = 1,
    ) -> str:
        if not spec.execute:
            return shlex.join(spec.command)
        if (
            self.config.container_runtime is None
            or self.config.container_image is None
        ):
            raise ValueError(
                "executable Slurm jobs require a bound container runtime "
                "and immutable container image"
            )
        if any(
            character in str(path)
            for path in spec.outputs
            for character in "*?[]"
        ):
            raise ValueError(
                "executable Slurm outputs must be explicit files"
            )
        parts = [
            shlex.quote(self.config.container_runtime),
            "exec",
            "--containall",
            "--cleanenv",
            "--no-home",
            "--net",
            "--network",
            "none",
            "--pwd",
            "/workspace",
            "--bind",
            shlex.quote(
                f"{self._execution_root(job_id, attempt)}:/workspace:ro"
            ),
        ]
        artifact_root = self._artifact_root(job_id, attempt)
        for output in spec.outputs:
            container_output = (
                Path("/workspace") / str(output)
            ).as_posix()
            parts.extend(
                [
                    "--bind",
                    shlex.quote(
                        f"{artifact_root / output}:{container_output}:rw"
                    ),
                ]
            )
        for key, value in sorted(spec.env.items()):
            parts.extend(["--env", shlex.quote(f"{key}={value}")])
        parts.append(shlex.quote(str(self.config.container_image)))
        parts.extend(shlex.quote(part) for part in spec.command)
        return " ".join(parts)

    def _prepare_execution_files(
        self,
        spec: JobSpec,
        job_id: str,
        attempt: int,
    ) -> None:
        if not spec.execute:
            return
        source_root = Path(spec.workdir).expanduser().resolve(strict=True)
        if not source_root.is_dir() or is_link_or_reparse_point(source_root):
            raise ValueError("Slurm source snapshot must be a regular directory")
        resolved_state = self.state_dir.expanduser().resolve(strict=False)
        if resolved_state == source_root or source_root in resolved_state.parents:
            raise ValueError("Slurm state directory must be outside the source snapshot")
        for source_entry in source_root.rglob("*"):
            if is_link_or_reparse_point(source_entry):
                raise ValueError("Slurm source snapshot contains a symbolic link")
        self._job_state_dir(job_id)
        attempt_root = self._artifact_root(job_id, attempt).parent
        if attempt_root.exists():
            if attempt_root.is_symlink() or not attempt_root.is_dir():
                raise ValueError("Slurm attempt path is unsafe")
            shutil.rmtree(attempt_root)
        safe_mkdir(attempt_root, anchor=self.state_dir)
        execution_root = self._execution_root(job_id, attempt)
        shutil.copytree(source_root, execution_root)
        if is_link_or_reparse_point(execution_root) or not execution_root.is_dir():
            raise ValueError("Slurm execution workspace is unsafe")
        artifact_root = safe_mkdir(
            self._artifact_root(job_id, attempt),
            anchor=self.state_dir,
        )
        for output in spec.outputs:
            source_output = execution_root / output
            artifact_output = artifact_root / output
            reject_symlink_components(source_output, anchor=execution_root)
            reject_symlink_components(artifact_output, anchor=artifact_root)
            safe_mkdir(source_output.parent, anchor=execution_root)
            safe_mkdir(artifact_output.parent, anchor=artifact_root)
            if is_link_or_reparse_point(
                source_output
            ) or is_link_or_reparse_point(artifact_output):
                raise ValueError("Slurm output path contains a symbolic link")
            if source_output.exists() and not source_output.is_file():
                raise ValueError("Slurm source output target is not a file")
            with source_output.open("wb"):
                pass
            with artifact_output.open("wb"):
                pass
            artifact_output.chmod(0o600)

    def submit(self, spec: JobSpec, *, execute: bool | None = None) -> JobResult:
        local_job_id = self._job_id(spec)
        attempt = 1
        argv = self._submit_argv(spec, local_job_id, attempt)
        plan = self._plan(
            job_id=local_job_id,
            action="submit",
            argv=argv,
            description=f"submit Slurm job {local_job_id}",
            environment_keys=tuple(spec.env),
            metadata={
                "slurm_job_name": self._submission_identity(local_job_id)[0],
                "slurm_job_comment": self._submission_identity(local_job_id)[1],
                "requested_job_name": spec.name,
            },
            sensitive_values=self._sensitive_environment_values(spec.env),
        )
        self._remember(local_job_id, spec=spec, result=plan)
        if not self._should_execute(spec, execute):
            return plan
        self._reject_sensitive_remote_environment(spec)
        self.policy.validate_command(argv, self.policy_action)
        self._job_state_dir(local_job_id)
        self._prepare_execution_files(spec, local_job_id, attempt)
        self._persist_submission_intent(spec, plan)
        outcome = self._run(spec, argv, timeout=60)
        raw_id = outcome.stdout.strip().split(";", 1)[0].strip()
        parsed = raw_id if raw_id.isdigit() else ""
        status = JobStatus.SUBMITTED if outcome.return_code == 0 and parsed else JobStatus.FAILED
        metadata = {
            "slurm_job_id": parsed or None,
            "local_job_id": local_job_id,
            "log_path_template": str(self._log_path(local_job_id, attempt)),
            "attempt": attempt,
        }
        result = JobResult(
            job_id=local_job_id,
            backend=self.name,
            status=status,
            executed=True,
            plan=plan.plan,
            return_code=outcome.return_code,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            message="Slurm job submitted"
            if status is JobStatus.SUBMITTED
            else "Slurm submit failed",
            metadata=metadata,
            created_at=plan.created_at,
        )
        self._slurm_ids[local_job_id] = parsed or local_job_id
        concrete_log = Path(
            str(self._log_path(local_job_id, attempt)).replace(
                "%j",
                parsed or local_job_id,
            )
        )
        self._log_paths[local_job_id] = concrete_log
        try:
            self._remember(local_job_id, spec=spec, result=result)
        except Exception:
            if status is JobStatus.SUBMITTED and parsed:
                self._run(
                    spec,
                    (self.config.scancel_executable, parsed),
                    timeout=60,
                )
            raise
        return result

    @staticmethod
    def _parse_reconciliation_rows(
        payload: str,
        *,
        expected_name: str,
        expected_comment: str,
    ) -> set[str]:
        matches: set[str] = set()
        for raw_line in payload.splitlines():
            fields = [field.strip() for field in raw_line.split("|")]
            if len(fields) < 3:
                continue
            raw_id, job_name, comment = fields[:3]
            scheduler_id = raw_id.split(".", 1)[0]
            if (
                scheduler_id.isdigit()
                and job_name == expected_name
                and comment == expected_comment
            ):
                matches.add(scheduler_id)
        return matches

    def _reconcile_scheduler_id(
        self,
        job_id: str,
        spec: JobSpec,
        previous: JobResult,
    ) -> str | None:
        expected_name, expected_comment = self._submission_identity(job_id)
        metadata = dict(previous.metadata)
        attempt = int(metadata.get("attempt") or 1)
        if metadata.get("slurm_job_name") not in {None, expected_name} or metadata.get(
            "slurm_job_comment"
        ) not in {None, expected_comment}:
            raise JobStateError("Slurm submission identity does not match the job")
        commands = (
            (
                self.config.squeue_executable,
                "--noheader",
                f"--name={expected_name}",
                "--format=%A|%j|%k",
            ),
            (
                self.config.sacct_executable,
                "--noheader",
                f"--name={expected_name}",
                "--format=JobIDRaw,JobName,Comment",
                "--parsable2",
                "--starttime=1970-01-01",
            ),
        )
        candidates: set[str] = set()
        for argv in commands:
            try:
                outcome = self._run(spec, argv, timeout=60)
            except FileNotFoundError:
                continue
            if outcome.return_code == 0:
                candidates.update(
                    self._parse_reconciliation_rows(
                        outcome.stdout,
                        expected_name=expected_name,
                        expected_comment=expected_comment,
                    )
                )
        if len(candidates) > 1:
            raise JobStateError(
                "Slurm submission recovery is ambiguous; multiple jobs match"
            )
        if not candidates:
            return None
        scheduler_id = next(iter(candidates))
        recovered_metadata = {
            **metadata,
            "submission_intent": False,
            "submission_reconciled": True,
            "slurm_job_id": scheduler_id,
            "local_job_id": job_id,
            "log_path_template": str(self._log_path(job_id, attempt)),
            "slurm_job_name": expected_name,
            "slurm_job_comment": expected_comment,
        }
        recovered = JobResult(
            job_id=job_id,
            backend=self.name,
            status=JobStatus.SUBMITTED,
            executed=True,
            plan=previous.plan,
            message="Slurm submission reconciled after interrupted persistence",
            metadata=recovered_metadata,
            created_at=previous.created_at,
        )
        self._remember(job_id, spec=spec, result=recovered)
        self._slurm_ids[job_id] = scheduler_id
        self._log_paths[job_id] = Path(
            str(self._log_path(job_id, attempt)).replace("%j", scheduler_id)
        )
        return scheduler_id

    def _scheduler_id(self, job_id: str, *, reconcile: bool = True) -> str:
        if job_id in self._slurm_ids:
            return self._slurm_ids[job_id]
        try:
            result = self._known_result(job_id)
        except KeyError:
            return job_id
        raw_scheduler_id = result.metadata.get("slurm_job_id")
        if isinstance(raw_scheduler_id, str) and raw_scheduler_id.isdigit():
            scheduler_id = raw_scheduler_id
        elif result.metadata.get("submission_intent") is True:
            if not reconcile:
                return job_id
            recovered_scheduler_id = self._reconcile_scheduler_id(
                job_id,
                self._known_spec(job_id),
                result,
            )
            if recovered_scheduler_id is None:
                raise JobStateError(
                    "Slurm submission intent could not yet be reconciled"
                )
            scheduler_id = recovered_scheduler_id
        else:
            scheduler_id = job_id
        self._slurm_ids[job_id] = scheduler_id
        return scheduler_id

    @staticmethod
    def _map_status(raw: str) -> JobStatus:
        normalized = raw.strip().upper().split()[0] if raw.strip() else ""
        normalized = normalized.split("+", 1)[0]
        mapping = {
            "PENDING": JobStatus.QUEUED,
            "CONFIGURING": JobStatus.QUEUED,
            "EXPEDITING": JobStatus.QUEUED,
            "POWER_UP_NODE": JobStatus.QUEUED,
            "REQUEUED": JobStatus.QUEUED,
            "REQUEUE_FED": JobStatus.QUEUED,
            "RUNNING": JobStatus.RUNNING,
            "COMPLETING": JobStatus.RUNNING,
            "RESIZING": JobStatus.RUNNING,
            "SIGNALING": JobStatus.RUNNING,
            "STAGE_OUT": JobStatus.RUNNING,
            "UPDATE_DB": JobStatus.RUNNING,
            "SUSPENDED": JobStatus.SUSPENDED,
            "REQUEUE_HOLD": JobStatus.SUSPENDED,
            "RESV_DEL_HOLD": JobStatus.SUSPENDED,
            "SPECIAL_EXIT": JobStatus.SUSPENDED,
            "STOPPED": JobStatus.SUSPENDED,
            "COMPLETED": JobStatus.SUCCEEDED,
            "BOOT_FAIL": JobStatus.FAILED,
            "DEADLINE": JobStatus.FAILED,
            "FAILED": JobStatus.FAILED,
            "LAUNCH_FAILED": JobStatus.FAILED,
            "TIMEOUT": JobStatus.FAILED,
            "OUT_OF_MEMORY": JobStatus.FAILED,
            "NODE_FAIL": JobStatus.FAILED,
            "RECONFIG_FAIL": JobStatus.FAILED,
            "CANCELLED": JobStatus.CANCELLED,
            "PREEMPTED": JobStatus.CANCELLED,
            "REVOKED": JobStatus.CANCELLED,
        }
        return mapping.get(normalized, JobStatus.UNKNOWN)

    def status(self, job_id: str, *, execute: bool = False) -> JobResult:
        spec = self._known_spec(job_id)
        try:
            scheduler_id = self._scheduler_id(job_id, reconcile=execute)
        except JobStateError as exc:
            persisted = self._known_result(job_id)
            return JobResult(
                job_id=job_id,
                backend=self.name,
                status=JobStatus.UNKNOWN,
                executed=execute,
                plan=persisted.plan,
                message=str(exc),
                metadata=persisted.metadata,
                created_at=persisted.created_at,
            )
        argv = (
            self.config.squeue_executable,
            "--noheader",
            "--jobs",
            scheduler_id,
            "--format=%T",
        )
        plan = self._plan(
            job_id=job_id,
            action="status",
            argv=argv,
            description=f"query Slurm job {scheduler_id}",
        )
        if not execute:
            return plan
        outcome = self._run(spec, argv, timeout=60)
        raw = outcome.stdout.strip() if outcome.return_code == 0 else ""
        stderr = outcome.stderr
        return_code = outcome.return_code
        if not raw:
            accounting_argv = (
                self.config.sacct_executable,
                "--noheader",
                "--jobs",
                scheduler_id,
                "--format=State",
                "--starttime=1970-01-01",
            )
            try:
                accounting = self._run(spec, accounting_argv, timeout=60)
            except FileNotFoundError as exc:
                accounting = None
                stderr += f"{exc}\n"
            if accounting is not None:
                raw = (
                    accounting.stdout.strip().splitlines()[0]
                    if accounting.return_code == 0 and accounting.stdout.strip()
                    else ""
                )
                stderr += accounting.stderr
                return_code = accounting.return_code
            if not raw:
                control_argv = (
                    self.config.scontrol_executable,
                    "show",
                    "job",
                    scheduler_id,
                    "--oneliner",
                )
                control = self._run(spec, control_argv, timeout=60)
                match = re.search(r"(?:^|\s)JobState=([A-Z_]+)", control.stdout)
                raw = match.group(1) if match else ""
                stderr += control.stderr
                return_code = control.return_code
        status = self._map_status(raw) if return_code == 0 else JobStatus.UNKNOWN
        previous = self._results.get(job_id)
        result = JobResult(
            job_id=job_id,
            backend=self.name,
            status=status,
            executed=True,
            plan=plan.plan,
            return_code=return_code,
            stdout=raw,
            stderr=stderr,
            message=f"Slurm job state: {status.value}",
            metadata=previous.metadata if previous else {"slurm_job_id": scheduler_id},
            created_at=previous.created_at if previous else plan.created_at,
        )
        self._remember(job_id, result=result)
        return result

    def cancel(self, job_id: str, *, execute: bool = False) -> JobResult:
        spec = self._known_spec(job_id)
        previous = self._known_result(job_id)
        scheduler_id = self._scheduler_id(job_id, reconcile=execute)
        argv = (self.config.scancel_executable, scheduler_id)
        plan = self._plan(
            job_id=job_id,
            action="cancel",
            argv=argv,
            description=f"cancel Slurm job {scheduler_id}",
        )
        if not execute:
            return plan
        outcome = self._run(spec, argv, timeout=60)
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
            message="Slurm job cancelled" if outcome.return_code == 0 else "Slurm cancel failed",
            metadata={
                **dict(previous.metadata),
                "slurm_job_id": scheduler_id,
            },
            created_at=previous.created_at,
        )
        self._remember(job_id, result=result)
        return result

    def resume(self, job_id: str, *, execute: bool = False) -> JobResult:
        spec = self._known_spec(job_id)
        previous = self._known_result(job_id)
        attempt = int(previous.metadata.get("attempt") or 1) + 1
        argv = self._submit_argv(spec, job_id, attempt)
        plan = self._plan(
            job_id=job_id,
            action="resume",
            argv=argv,
            description=f"submit fresh Slurm attempt {attempt} for {job_id}",
        )
        if not execute:
            return plan
        if previous.status in {
            JobStatus.SUBMITTED,
            JobStatus.QUEUED,
            JobStatus.RUNNING,
            JobStatus.SUSPENDED,
        }:
            raise JobStateError(f"cannot resume active Slurm job {job_id}")
        self._prepare_execution_files(spec, job_id, attempt)
        outcome = self._run(spec, argv, timeout=60)
        raw_id = outcome.stdout.strip().split(";", 1)[0].strip()
        scheduler_id = raw_id if raw_id.isdigit() else ""
        status = (
            JobStatus.QUEUED
            if outcome.return_code == 0 and scheduler_id
            else JobStatus.FAILED
        )
        metadata = {
            "slurm_job_id": scheduler_id or None,
            "local_job_id": job_id,
            "log_path_template": str(self._log_path(job_id, attempt)),
            "slurm_job_name": self._submission_identity(job_id)[0],
            "slurm_job_comment": self._submission_identity(job_id)[1],
            "attempt": attempt,
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
            message=(
                "fresh Slurm attempt submitted"
                if status is JobStatus.QUEUED
                else "Slurm resume failed"
            ),
            metadata=metadata,
            created_at=previous.created_at,
        )
        if scheduler_id:
            self._slurm_ids[job_id] = scheduler_id
            self._log_paths[job_id] = Path(
                str(self._log_path(job_id, attempt)).replace(
                    "%j",
                    scheduler_id,
                )
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
        log_path = self._log_paths.get(job_id)
        if log_path is None:
            previous = self._known_result(job_id)
            raw_log = previous.metadata.get("log_path_template")
            template = (
                Path(str(raw_log))
                if isinstance(raw_log, str) and raw_log
                else self._log_path(job_id)
            )
            log_path = Path(
                str(template).replace(
                    "%j",
                    self._scheduler_id(job_id, reconcile=execute),
                )
            )
            self._log_paths[job_id] = log_path
        argv: tuple[str, ...] = (
            ("cat", str(log_path))
            if tail is None
            else ("tail", "-n", str(tail), str(log_path))
        )
        if follow:
            argv = ("tail", "-f", str(log_path))
        plan = self._plan(
            job_id=job_id,
            action="logs",
            argv=argv,
            description=f"read Slurm log for {job_id}",
        )
        if not execute:
            return plan
        outcome = self._run(spec, argv, timeout=60)
        current_result = self._results.get(job_id)
        return JobResult(
            job_id=job_id,
            backend=self.name,
            status=(
                current_result.status
                if current_result
                else JobStatus.UNKNOWN
            ),
            executed=True,
            plan=plan.plan,
            return_code=outcome.return_code,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            message="Slurm log snapshot",
            metadata=current_result.metadata if current_result else {},
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
        previous = self._known_result(job_id)
        raw_attempt = previous.metadata.get("attempt")
        attempt = (
            int(raw_attempt)
            if isinstance(raw_attempt, int) and not isinstance(raw_attempt, bool)
            else 1
        )
        workdir = (
            self._artifact_root(job_id, attempt)
            if spec.execute
            else Path(spec.workdir).expanduser().resolve()
        )
        local_root = Path(local_path).expanduser().resolve()
        if direction is ArtifactDirection.DOWNLOAD:
            source_root, destination_root = workdir, local_root
        else:
            source_root, destination_root = local_root, workdir
        plan = self._plan(
            job_id=job_id,
            action="artifact-sync",
            argv=(
                "paperforge-slurm-copy",
                direction.value,
                str(source_root),
                str(destination_root),
                *selected,
            ),
            description=f"{direction.value} Slurm artifacts for {job_id}",
        )
        if not execute:
            return plan
        self.policy.require(self.policy_action, detail=f"Slurm artifact sync {job_id}")
        try:
            artifacts = copy_local_artifacts(
                source_root=source_root,
                destination_root=destination_root,
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
                message="Slurm artifact sync failed",
            )
        return JobResult(
            job_id=job_id,
            backend=self.name,
            status=JobStatus.SUCCEEDED,
            executed=True,
            plan=plan.plan,
            return_code=0,
            artifacts=artifacts,
            message=f"synced {len(artifacts)} Slurm artifact files",
            metadata={"attempt": attempt, "paths": list(selected)},
        )


SlurmComputeBackend = SlurmBackend
