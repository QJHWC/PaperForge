from __future__ import annotations

import re
import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from paperforge.policy import Action

from ._artifacts import artifact_patterns, copy_local_artifacts
from .base import ComputeBackend
from .contracts import ArtifactDirection, JobResult, JobSpec, JobStatus

_COMMAND_PATTERN = re.compile(r"^[A-Za-z0-9_./-]+$")


@dataclass(frozen=True)
class SlurmConfig:
    sbatch_executable: str = "sbatch"
    squeue_executable: str = "squeue"
    sacct_executable: str = "sacct"
    scancel_executable: str = "scancel"
    scontrol_executable: str = "scontrol"
    default_partition: str | None = None
    extra_submit_args: tuple[str, ...] = ()

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
        if any("\x00" in value for value in self.extra_submit_args):
            raise ValueError("extra_submit_args contains a NUL byte")
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

    def _log_path(self, job_id: str) -> Path:
        return (self.state_dir / self.name / job_id / "slurm-%j.log").resolve()

    @staticmethod
    def _duration(seconds: int) -> str:
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def _submit_argv(self, spec: JobSpec, job_id: str) -> tuple[str, ...]:
        log_path = self._log_path(job_id)
        argv: list[str] = [
            self.config.sbatch_executable,
            "--parsable",
            f"--job-name={spec.name}",
            f"--cpus-per-task={spec.resources.cpus}",
            f"--chdir={spec.workdir}",
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
        rendered = shlex.join(spec.command)
        if environment:
            rendered = f"env {environment} {rendered}"
        argv.extend(["--wrap", rendered])
        return tuple(argv)

    def submit(self, spec: JobSpec, *, execute: bool | None = None) -> JobResult:
        local_job_id = self._job_id(spec)
        argv = self._submit_argv(spec, local_job_id)
        plan = self._plan(
            job_id=local_job_id,
            action="submit",
            argv=argv,
            description=f"submit Slurm job {local_job_id}",
            environment_keys=tuple(spec.env),
            sensitive_values=self._sensitive_environment_values(spec.env),
        )
        self._remember(local_job_id, spec=spec, result=plan)
        if not self._should_execute(spec, execute):
            return plan
        self._reject_sensitive_remote_environment(spec)
        self.policy.validate_command(argv, self.policy_action)
        self._log_path(local_job_id).parent.mkdir(parents=True, exist_ok=True)
        outcome = self._run(spec, argv, timeout=60)
        raw_id = outcome.stdout.strip().split(";", 1)[0].strip()
        parsed = raw_id if raw_id.isdigit() else ""
        job_id = parsed or local_job_id
        status = JobStatus.SUBMITTED if outcome.return_code == 0 and parsed else JobStatus.FAILED
        metadata = {
            "slurm_job_id": parsed or None,
            "local_job_id": local_job_id,
            "log_path_template": str(self._log_path(local_job_id)),
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
            message="Slurm job submitted"
            if status is JobStatus.SUBMITTED
            else "Slurm submit failed",
            metadata=metadata,
            created_at=plan.created_at,
        )
        self._remember(job_id, spec=spec, result=result)
        self._slurm_ids[job_id] = parsed or job_id
        concrete_log = Path(str(self._log_path(local_job_id)).replace("%j", parsed or job_id))
        self._log_paths[job_id] = concrete_log
        return result

    def _scheduler_id(self, job_id: str) -> str:
        return self._slurm_ids.get(job_id, job_id)

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
        scheduler_id = self._scheduler_id(job_id)
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
        scheduler_id = self._scheduler_id(job_id)
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
            metadata={"slurm_job_id": scheduler_id},
        )
        self._remember(job_id, result=result)
        return result

    def resume(self, job_id: str, *, execute: bool = False) -> JobResult:
        spec = self._known_spec(job_id)
        scheduler_id = self._scheduler_id(job_id)
        argv = (self.config.scontrol_executable, "requeue", scheduler_id)
        plan = self._plan(
            job_id=job_id,
            action="resume",
            argv=argv,
            description=f"requeue Slurm job {scheduler_id}",
        )
        if not execute:
            return plan
        outcome = self._run(spec, argv, timeout=60)
        status = JobStatus.QUEUED if outcome.return_code == 0 else JobStatus.FAILED
        result = JobResult(
            job_id=job_id,
            backend=self.name,
            status=status,
            executed=True,
            plan=plan.plan,
            return_code=outcome.return_code,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            message="Slurm job requeued" if outcome.return_code == 0 else "Slurm requeue failed",
            metadata={"slurm_job_id": scheduler_id},
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
        log_path = self._log_paths.get(job_id, self._log_path(job_id))
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
            message="Slurm log snapshot",
            metadata=previous.metadata if previous else {},
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
        )


SlurmComputeBackend = SlurmBackend
