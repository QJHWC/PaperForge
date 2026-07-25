from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from paperforge.policy import Action

from ._artifacts import artifact_patterns
from .base import ComputeBackend
from .contracts import ArtifactDirection, JobResult, JobSpec, JobStatus

_DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@-]*$")


@dataclass(frozen=True)
class KubernetesConfig:
    image: str
    namespace: str = "default"
    context: str | None = None
    kubectl_executable: str = "kubectl"
    container_name: str = "job"
    container_workdir: str = "/workspace"
    service_account: str | None = None
    image_pull_policy: str = "IfNotPresent"
    backoff_limit: int = 0
    ttl_seconds_after_finished: int | None = 86400

    def __post_init__(self) -> None:
        if not self.image or "\x00" in self.image:
            raise ValueError("Kubernetes image must be non-empty")
        if not _DNS_LABEL.fullmatch(self.namespace):
            raise ValueError("Kubernetes namespace must be a DNS label")
        if self.context is not None and not _SAFE_VALUE.fullmatch(self.context):
            raise ValueError("Kubernetes context contains unsafe characters")
        if not re.fullmatch(r"[A-Za-z0-9_./-]+", self.kubectl_executable):
            raise ValueError("kubectl_executable contains unsafe characters")
        if not _DNS_LABEL.fullmatch(self.container_name):
            raise ValueError("container_name must be a DNS label")
        workdir = PurePosixPath(self.container_workdir)
        if not workdir.is_absolute() or ".." in workdir.parts:
            raise ValueError("container_workdir must be absolute and traversal-free")
        if self.service_account is not None and not _DNS_LABEL.fullmatch(self.service_account):
            raise ValueError("service_account must be a DNS label")
        if self.image_pull_policy not in {"Always", "IfNotPresent", "Never"}:
            raise ValueError("invalid image_pull_policy")
        if isinstance(self.backoff_limit, bool) or self.backoff_limit < 0:
            raise ValueError("backoff_limit must be non-negative")
        if self.ttl_seconds_after_finished is not None and (
            isinstance(self.ttl_seconds_after_finished, bool) or self.ttl_seconds_after_finished < 0
        ):
            raise ValueError("ttl_seconds_after_finished must be non-negative")


class KubernetesBackend(ComputeBackend):
    name = "kubernetes"
    policy_action = Action.REMOTE_EXECUTE

    def __init__(
        self,
        config: KubernetesConfig | None = None,
        *,
        image: str | None = None,
        **kwargs: Any,
    ) -> None:
        if config is not None and image is not None:
            raise TypeError("pass either KubernetesConfig or image, not both")
        self.config = config or KubernetesConfig(image=image or "")
        super().__init__(**kwargs)
        self._remote_names: dict[str, str] = {}
        self._manifest_paths: dict[str, Path] = {}

    @staticmethod
    def _job_name(job_id: str) -> str:
        normalized = re.sub(r"[^a-z0-9-]+", "-", job_id.lower()).strip("-")
        normalized = re.sub(r"-+", "-", normalized)
        digest = hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:10]
        prefix = normalized[: 63 - len(digest) - 1].rstrip("-")
        return f"{prefix or 'paperforge-job'}-{digest}"

    def _kubectl(self, *args: str) -> tuple[str, ...]:
        argv: list[str] = [self.config.kubectl_executable]
        if self.config.context is not None:
            argv.extend(["--context", self.config.context])
        argv.extend(["--namespace", self.config.namespace])
        argv.extend(args)
        return tuple(argv)

    def _manifest_path(self, job_id: str) -> Path:
        return (self.state_dir / self.name / job_id / "job.json").resolve()

    def _manifest(self, spec: JobSpec, job_id: str) -> dict[str, Any]:
        remote_name = self._job_name(job_id)
        resources: dict[str, dict[str, str]] = {"requests": {"cpu": str(spec.resources.cpus)}}
        if spec.resources.memory_mb is not None:
            resources["requests"]["memory"] = f"{spec.resources.memory_mb}Mi"
        if spec.resources.gpus:
            resources["requests"]["nvidia.com/gpu"] = str(spec.resources.gpus)
        resources["limits"] = dict(resources["requests"])
        container: dict[str, Any] = {
            "name": self.config.container_name,
            "image": str(spec.backend_options.get("image", self.config.image)),
            "imagePullPolicy": self.config.image_pull_policy,
            "workingDir": str(
                spec.backend_options.get("container_workdir", self.config.container_workdir)
            ),
            "command": list(spec.command),
            "env": [{"name": key, "value": value} for key, value in sorted(spec.env.items())],
            "resources": resources,
        }
        pod_spec: dict[str, Any] = {
            "restartPolicy": "Never",
            "containers": [container],
        }
        if self.config.service_account:
            pod_spec["serviceAccountName"] = self.config.service_account
        job_spec: dict[str, Any] = {
            "backoffLimit": self.config.backoff_limit,
            "template": {
                "metadata": {"labels": {"paperforge-job": remote_name}},
                "spec": pod_spec,
            },
        }
        if self.config.ttl_seconds_after_finished is not None:
            job_spec["ttlSecondsAfterFinished"] = self.config.ttl_seconds_after_finished
        if spec.resources.timeout_seconds is not None:
            job_spec["activeDeadlineSeconds"] = spec.resources.timeout_seconds
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": remote_name,
                "namespace": self.config.namespace,
                "labels": {
                    "app.kubernetes.io/managed-by": "paperforge",
                    "paperforge-job": remote_name,
                },
            },
            "spec": job_spec,
        }

    def _apply_argv(self, job_id: str) -> tuple[str, ...]:
        return self._kubectl("apply", "-f", str(self._manifest_path(job_id)))

    def _write_manifest(self, spec: JobSpec, job_id: str) -> Path:
        path = self._manifest_path(job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self._manifest(spec, job_id), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(path)
        self._manifest_paths[job_id] = path
        return path

    def submit(self, spec: JobSpec, *, execute: bool | None = None) -> JobResult:
        job_id = self._job_id(spec)
        remote_name = self._job_name(job_id)
        argv = self._apply_argv(job_id)
        manifest = self._manifest(spec, job_id)
        plan = self._plan(
            job_id=job_id,
            action="submit",
            argv=argv,
            description=f"apply Kubernetes Job {remote_name}",
            environment_keys=tuple(spec.env),
            metadata={"remote_name": remote_name, "manifest": manifest},
            sensitive_values=self._sensitive_environment_values(spec.env),
        )
        self._remote_names[job_id] = remote_name
        self._remember(job_id, spec=spec, result=plan)
        if not self._should_execute(spec, execute):
            return plan
        self._reject_sensitive_remote_environment(spec)
        self.policy.validate_command(argv, self.policy_action)
        self._write_manifest(spec, job_id)
        outcome = self._run(spec, argv, timeout=60)
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
            message=(
                "Kubernetes job submitted"
                if outcome.return_code == 0
                else "Kubernetes submit failed"
            ),
            metadata={
                "remote_name": remote_name,
                "namespace": self.config.namespace,
                "manifest_path": str(self._manifest_path(job_id)),
            },
            created_at=plan.created_at,
        )
        self._remember(job_id, result=result)
        return result

    def _remote_name(self, job_id: str) -> str:
        return self._remote_names.get(job_id, self._job_name(job_id))

    @staticmethod
    def _status_from_job(payload: dict[str, Any]) -> JobStatus:
        status = payload.get("status") or {}
        conditions = status.get("conditions") or []
        for condition in conditions:
            if condition.get("status") != "True":
                continue
            if condition.get("type") == "Complete":
                return JobStatus.SUCCEEDED
            if condition.get("type") == "Failed":
                return JobStatus.FAILED
            if condition.get("type") == "Suspended":
                return JobStatus.SUSPENDED
        if int(status.get("active") or 0) > 0:
            return JobStatus.RUNNING
        if int(status.get("ready") or 0) > 0:
            return JobStatus.RUNNING
        if not status.get("startTime"):
            return JobStatus.QUEUED
        return JobStatus.UNKNOWN

    def status(self, job_id: str, *, execute: bool = False) -> JobResult:
        spec = self._known_spec(job_id)
        remote_name = self._remote_name(job_id)
        argv = self._kubectl("get", "job", remote_name, "-o", "json")
        plan = self._plan(
            job_id=job_id,
            action="status",
            argv=argv,
            description=f"inspect Kubernetes Job {remote_name}",
        )
        if not execute:
            return plan
        outcome = self._run(spec, argv, timeout=60)
        payload: dict[str, Any] = {}
        if outcome.return_code == 0:
            with suppress(json.JSONDecodeError):
                payload = json.loads(outcome.stdout)
        status = self._status_from_job(payload) if payload else JobStatus.UNKNOWN
        previous = self._results.get(job_id)
        result = JobResult(
            job_id=job_id,
            backend=self.name,
            status=status,
            executed=True,
            plan=plan.plan,
            return_code=outcome.return_code,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            message=f"Kubernetes job state: {status.value}",
            metadata=previous.metadata if previous else {"remote_name": remote_name},
            created_at=previous.created_at if previous else plan.created_at,
        )
        self._remember(job_id, result=result)
        return result

    def cancel(self, job_id: str, *, execute: bool = False) -> JobResult:
        spec = self._known_spec(job_id)
        remote_name = self._remote_name(job_id)
        argv = self._kubectl(
            "delete",
            "job",
            remote_name,
            "--ignore-not-found=true",
            "--wait=true",
        )
        plan = self._plan(
            job_id=job_id,
            action="cancel",
            argv=argv,
            description=f"delete Kubernetes Job {remote_name}",
        )
        if not execute:
            return plan
        outcome = self._run(spec, argv, timeout=120)
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
            message=(
                "Kubernetes job cancelled"
                if outcome.return_code == 0
                else "Kubernetes cancel failed"
            ),
            metadata={"remote_name": remote_name, "namespace": self.config.namespace},
        )
        self._remember(job_id, result=result)
        return result

    def resume(self, job_id: str, *, execute: bool = False) -> JobResult:
        spec = self._known_spec(job_id)
        remote_name = self._remote_name(job_id)
        delete_argv = self._kubectl(
            "delete",
            "job",
            remote_name,
            "--ignore-not-found=true",
            "--wait=true",
        )
        apply_argv = self._apply_argv(job_id)
        plan = self._plan(
            job_id=job_id,
            action="resume",
            argv=apply_argv,
            description=f"recreate Kubernetes Job {remote_name}",
            metadata={
                "commands": [list(delete_argv), list(apply_argv)],
                "manifest": self._manifest(spec, job_id),
            },
            sensitive_values=self._sensitive_environment_values(spec.env),
        )
        if not execute:
            return plan
        self._reject_sensitive_remote_environment(spec)
        self.policy.validate_command(apply_argv, self.policy_action)
        self._write_manifest(spec, job_id)
        deleted = self._run(spec, delete_argv, timeout=120)
        outcome = deleted if deleted.return_code != 0 else self._run(spec, apply_argv, timeout=60)
        status = JobStatus.SUBMITTED if outcome.return_code == 0 else JobStatus.FAILED
        result = JobResult(
            job_id=job_id,
            backend=self.name,
            status=status,
            executed=True,
            plan=plan.plan,
            return_code=outcome.return_code,
            stdout=deleted.stdout + (outcome.stdout if outcome is not deleted else ""),
            stderr=deleted.stderr + (outcome.stderr if outcome is not deleted else ""),
            message=(
                "Kubernetes job recreated"
                if outcome.return_code == 0
                else "Kubernetes resume failed"
            ),
            metadata={"remote_name": remote_name, "namespace": self.config.namespace},
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
        remote_name = self._remote_name(job_id)
        argv: list[str] = list(self._kubectl("logs", f"job/{remote_name}"))
        if tail is not None:
            argv.append(f"--tail={tail}")
        if follow:
            argv.append("--follow=true")
        plan = self._plan(
            job_id=job_id,
            action="logs",
            argv=argv,
            description=f"read Kubernetes logs for {remote_name}",
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
            message="Kubernetes log snapshot",
            metadata=previous.metadata if previous else {"remote_name": remote_name},
        )

    def _pod_name(self, spec: JobSpec, remote_name: str) -> tuple[str, str, str]:
        argv = self._kubectl(
            "get",
            "pods",
            "-l",
            f"job-name={remote_name}",
            "-o",
            "jsonpath={.items[0].metadata.name}",
        )
        outcome = self._run(spec, argv, timeout=60)
        return outcome.stdout.strip(), outcome.stderr, str(outcome.return_code)

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
        remote_name = self._remote_name(job_id)
        workdir = PurePosixPath(
            str(spec.backend_options.get("container_workdir", self.config.container_workdir))
        )
        resolution_argv = self._kubectl(
            "get",
            "pods",
            "-l",
            f"job-name={remote_name}",
            "-o",
            "jsonpath={.items[0].metadata.name}",
        )
        plan = self._plan(
            job_id=job_id,
            action="artifact-sync",
            argv=resolution_argv,
            description=f"{direction.value} Kubernetes artifacts for {remote_name}",
            metadata={
                "paths": list(selected),
                "pod_resolution": "resolve the concrete pod before kubectl cp",
                "direction": direction.value,
                "container_workdir": workdir.as_posix(),
            },
        )
        if not execute:
            return plan
        if direction is ArtifactDirection.DOWNLOAD:
            local_root.mkdir(parents=True, exist_ok=True)
        pod, resolution_stderr, resolution_code = self._pod_name(spec, remote_name)
        if resolution_code != "0" or not pod:
            return JobResult(
                job_id=job_id,
                backend=self.name,
                status=JobStatus.FAILED,
                executed=True,
                plan=plan.plan,
                return_code=int(resolution_code),
                stderr=resolution_stderr or "no pod found for Kubernetes job",
                message="Kubernetes artifact sync could not resolve a pod",
            )
        stdout: list[str] = []
        stderr: list[str] = []
        return_code = 0
        for pattern in selected:
            remote = (workdir / pattern).as_posix()
            local_target = local_root / pattern
            if direction is ArtifactDirection.DOWNLOAD:
                local_target.parent.mkdir(parents=True, exist_ok=True)
                argv = self._kubectl(
                    "cp",
                    f"{self.config.namespace}/{pod}:{remote}",
                    str(local_target),
                )
            else:
                argv = self._kubectl(
                    "cp",
                    str(local_target),
                    f"{self.config.namespace}/{pod}:{remote}",
                )
            outcome = self._run(spec, argv, timeout=spec.resources.timeout_seconds)
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
                "Kubernetes artifact sync completed"
                if return_code == 0
                else "Kubernetes artifact sync failed"
            ),
            metadata={"pod": pod, "paths": list(selected), "direction": direction.value},
        )


KubernetesComputeBackend = KubernetesBackend
