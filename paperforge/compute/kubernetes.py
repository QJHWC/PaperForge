from __future__ import annotations

import hashlib
import json
import re
import shlex
import shutil
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from paperforge.path_safety import reject_symlink_components, safe_mkdir
from paperforge.policy import Action

from ._artifacts import (
    artifact_patterns,
    copy_local_artifacts,
    file_record,
    safe_artifact_destination,
    safe_artifact_file,
    safe_artifact_root,
)
from .base import ComputeBackend
from .contracts import (
    ArtifactDirection,
    ArtifactRecord,
    JobResult,
    JobSpec,
    JobStatus,
)
from .source_bundle import (
    SourceBundleError,
    create_verified_source_bundle,
)

_DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@-]*$")
_IMAGE_DIGEST = re.compile(r"^.+@sha256:[0-9a-fA-F]{64}$")
_OUTPUT_MOUNT_ROOT = PurePosixPath("/paperforge-outputs")


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
    source_pvc: str | None = None
    artifact_pvc: str | None = None
    source_transport_image: str | None = None
    run_as_user: int = 65532
    run_as_group: int = 65532
    fs_group: int = 65532

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
        for label, value in (
            ("source_pvc", self.source_pvc),
            ("artifact_pvc", self.artifact_pvc),
        ):
            if value is not None and not _DNS_LABEL.fullmatch(value):
                raise ValueError(f"{label} must be a DNS label")
        if self.source_transport_image is not None and not (
            _IMAGE_DIGEST.fullmatch(self.source_transport_image)
        ):
            raise ValueError(
                "source_transport_image must be pinned by sha256 digest"
            )
        for numeric_label, numeric_value in (
            ("run_as_user", self.run_as_user),
            ("run_as_group", self.run_as_group),
            ("fs_group", self.fs_group),
        ):
            if (
                isinstance(numeric_value, bool)
                or not isinstance(numeric_value, int)
                or numeric_value < 1
            ):
                raise ValueError(f"{numeric_label} must be a positive integer")


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

    def _manifest_path(self, job_id: str, attempt: int = 1) -> Path:
        return self._job_state_path(job_id, f"job-attempt-{attempt}.json")

    def _source_manifest_path(self, job_id: str) -> Path:
        return self._job_state_path(job_id, "source-pod.json")

    def _artifact_manifest_path(self, job_id: str, attempt: int = 1) -> Path:
        return self._job_state_path(
            job_id,
            f"artifact-pod-attempt-{attempt}.json",
        )

    @staticmethod
    def _deny_network_policy(
        *,
        name: str,
        namespace: str,
        labels: dict[str, str],
    ) -> dict[str, Any]:
        return {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": name, "namespace": namespace},
            "spec": {
                "podSelector": {"matchLabels": labels},
                "policyTypes": ["Ingress", "Egress"],
                "ingress": [],
                "egress": [],
            },
        }

    def _network_policy_name(self, job_id: str) -> str:
        return self._job_name(f"{job_id}-network")

    def _attempt_remote_name(self, job_id: str, attempt: int) -> str:
        return self._job_name(f"{job_id}-attempt-{attempt}")

    def _manifest(
        self,
        spec: JobSpec,
        job_id: str,
        attempt: int = 1,
    ) -> dict[str, Any]:
        remote_name = self._attempt_remote_name(job_id, attempt)
        container_workdir = PurePosixPath(
            str(
                spec.backend_options.get(
                    "container_workdir",
                    self.config.container_workdir,
                )
            )
        )
        if not container_workdir.is_absolute() or ".." in container_workdir.parts:
            raise ValueError(
                "backend container_workdir must be absolute and traversal-free"
            )
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
            "workingDir": container_workdir.as_posix(),
            "command": list(spec.command),
            "env": [{"name": key, "value": value} for key, value in sorted(spec.env.items())],
            "resources": resources,
            "securityContext": {
                "allowPrivilegeEscalation": False,
                "capabilities": {"drop": ["ALL"]},
                "readOnlyRootFilesystem": True,
                "runAsNonRoot": True,
                "runAsUser": self.config.run_as_user,
                "runAsGroup": self.config.run_as_group,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
        }
        pod_spec: dict[str, Any] = {
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "containers": [container],
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": self.config.run_as_user,
                "runAsGroup": self.config.run_as_group,
                "fsGroup": self.config.fs_group,
                "fsGroupChangePolicy": "OnRootMismatch",
                "seccompProfile": {"type": "RuntimeDefault"},
            },
        }
        if spec.execute:
            if (
                self.config.source_pvc is None
                or self.config.artifact_pvc is None
                or self.config.source_transport_image is None
            ):
                raise ValueError(
                    "executable Kubernetes jobs require source_pvc, "
                    "artifact_pvc, and pinned source_transport_image"
                )
            if any(
                character in str(path)
                for path in spec.outputs
                for character in "*?[]"
            ):
                raise ValueError(
                    "executable Kubernetes outputs must be explicit files"
                )
            pod_spec["volumes"] = [
                {
                    "name": "paperforge-source",
                    "persistentVolumeClaim": {
                        "claimName": self.config.source_pvc,
                    },
                },
                {
                    "name": "paperforge-artifacts",
                    "persistentVolumeClaim": {
                        "claimName": self.config.artifact_pvc,
                    },
                },
            ]
            volume_mounts = [
                {
                    "name": "paperforge-source",
                    "mountPath": container_workdir.as_posix(),
                    "subPath": f"sources/{job_id}",
                    "readOnly": True,
                },
                {
                    "name": "paperforge-output-mounts",
                    "mountPath": _OUTPUT_MOUNT_ROOT.as_posix(),
                },
            ]
            for index, output in enumerate(spec.outputs):
                volume_mounts.append(
                    {
                        "name": "paperforge-artifacts",
                        "mountPath": (
                            _OUTPUT_MOUNT_ROOT / str(index)
                        ).as_posix(),
                        "subPath": (
                            f"artifacts/{job_id}/attempts/{attempt}/{output}"
                        ),
                        "readOnly": False,
                    }
                )
            container["volumeMounts"] = volume_mounts
            pod_spec["volumes"].append(
                {"name": "paperforge-output-mounts", "emptyDir": {}}
            )
            artifact_attempt_root = (
                PurePosixPath("/pvc-artifacts/artifacts")
                / job_id
                / "attempts"
                / str(attempt)
            )
            preparation: list[str] = []
            current = PurePosixPath("/")
            for part in artifact_attempt_root.parent.parts[1:]:
                current /= part
                rendered = shlex.quote(current.as_posix())
                preparation.append(
                    f"[ ! -L {rendered} ] && mkdir -p {rendered}"
                )
            rendered_attempt = shlex.quote(artifact_attempt_root.as_posix())
            preparation.extend(
                [
                    f"[ ! -L {rendered_attempt} ]",
                    f"rm -rf {rendered_attempt}",
                    f"mkdir -p {rendered_attempt}",
                ]
            )
            for output in spec.outputs:
                target = artifact_attempt_root / output
                preparation.extend(
                    [
                        f"mkdir -p {shlex.quote(target.parent.as_posix())}",
                        f": > {shlex.quote(target.as_posix())}",
                        f"chmod u+rw,go-rwx {shlex.quote(target.as_posix())}",
                    ]
                )
            pod_spec["initContainers"] = [
                {
                    "name": "artifact-prepare",
                    "image": self.config.source_transport_image,
                    "imagePullPolicy": self.config.image_pull_policy,
                    "command": ["sh", "-c", " && ".join(preparation)],
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "capabilities": {"drop": ["ALL"]},
                        "readOnlyRootFilesystem": True,
                        "runAsNonRoot": True,
                        "runAsUser": self.config.run_as_user,
                        "runAsGroup": self.config.run_as_group,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "volumeMounts": [
                        {
                            "name": "paperforge-artifacts",
                            "mountPath": "/pvc-artifacts",
                        }
                    ],
                },
            ]
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
        job = {
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
        return {
            "apiVersion": "v1",
            "kind": "List",
            "items": [
                self._deny_network_policy(
                    name=self._network_policy_name(
                        f"{job_id}-attempt-{attempt}"
                    ),
                    namespace=self.config.namespace,
                    labels={"paperforge-job": remote_name},
                ),
                job,
            ],
        }

    def _apply_argv(self, job_id: str, attempt: int = 1) -> tuple[str, ...]:
        return self._kubectl(
            "apply",
            "-f",
            str(self._manifest_path(job_id, attempt)),
        )

    def _policy_manifest_path(self, job_id: str, attempt: int) -> Path:
        return self._job_state_path(
            job_id,
            f"network-policy-attempt-{attempt}.json",
        )

    def _job_resource_path(self, job_id: str, attempt: int) -> Path:
        return self._job_state_path(
            job_id,
            f"job-resource-attempt-{attempt}.json",
        )

    def _execution_apply_commands(
        self,
        job_id: str,
        attempt: int,
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        policy_name = self._network_policy_name(
            f"{job_id}-attempt-{attempt}"
        )
        return (
            self._kubectl(
                "apply",
                "-f",
                str(self._policy_manifest_path(job_id, attempt)),
            ),
            self._kubectl(
                "get",
                "networkpolicy",
                policy_name,
                "-o",
                "jsonpath={.metadata.uid}",
            ),
            self._kubectl(
                "apply",
                "-f",
                str(self._job_resource_path(job_id, attempt)),
            ),
        )

    def _write_manifest(
        self,
        spec: JobSpec,
        job_id: str,
        attempt: int = 1,
    ) -> Path:
        path = self._manifest_path(job_id, attempt)
        written = self._write_payload(
            path,
            self._manifest(spec, job_id, attempt),
        )
        self._manifest_paths[job_id] = written
        return written

    def _write_execution_manifests(
        self,
        spec: JobSpec,
        job_id: str,
        attempt: int,
    ) -> tuple[Path, Path, Path]:
        payload = self._manifest(spec, job_id, attempt)
        policy, job = payload["items"]
        combined = self._write_manifest(spec, job_id, attempt)
        policy_path = self._write_payload(
            self._policy_manifest_path(job_id, attempt),
            policy,
        )
        job_path = self._write_payload(
            self._job_resource_path(job_id, attempt),
            job,
        )
        return combined, policy_path, job_path

    def _write_payload(
        self,
        path: Path,
        payload: dict[str, Any],
    ) -> Path:
        safe_mkdir(self.state_dir)
        safe_mkdir(path.parent, anchor=self.state_dir)
        reject_symlink_components(path, anchor=self.state_dir)
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError("Kubernetes manifest path is unsafe")
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(path)
        return path

    def _source_pod_manifest(
        self,
        job_id: str,
    ) -> dict[str, Any]:
        if (
            self.config.source_pvc is None
            or self.config.artifact_pvc is None
            or self.config.source_transport_image is None
        ):
            raise ValueError(
                "Kubernetes source transport is not configured"
            )
        pod_name = self._job_name(f"{job_id}-source")
        source_labels = {
            "app.kubernetes.io/managed-by": "paperforge",
            "paperforge-source-job": self._job_name(job_id),
        }
        pod = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": pod_name,
                "namespace": self.config.namespace,
                "labels": source_labels,
            },
            "spec": {
                "restartPolicy": "Never",
                "automountServiceAccountToken": False,
                "securityContext": {
                    "runAsNonRoot": True,
                    "runAsUser": self.config.run_as_user,
                    "runAsGroup": self.config.run_as_group,
                    "fsGroup": self.config.fs_group,
                    "fsGroupChangePolicy": "OnRootMismatch",
                    "seccompProfile": {"type": "RuntimeDefault"},
                },
                "containers": [
                    {
                        "name": "source-transport",
                        "image": self.config.source_transport_image,
                        "imagePullPolicy": self.config.image_pull_policy,
                        "command": [
                            "sh",
                            "-c",
                            "trap : TERM INT; sleep 600 & wait",
                        ],
                        "securityContext": {
                            "allowPrivilegeEscalation": False,
                            "capabilities": {"drop": ["ALL"]},
                            "readOnlyRootFilesystem": True,
                            "runAsNonRoot": True,
                            "runAsUser": self.config.run_as_user,
                            "runAsGroup": self.config.run_as_group,
                            "seccompProfile": {
                                "type": "RuntimeDefault"
                            },
                        },
                        "volumeMounts": [
                            {
                                "name": "source",
                                "mountPath": "/pvc-source",
                            },
                            {
                                "name": "artifacts",
                                "mountPath": "/pvc-artifacts",
                            },
                            {
                                "name": "temporary",
                                "mountPath": "/tmp",
                            },
                        ],
                    }
                ],
                "volumes": [
                    {
                        "name": "source",
                        "persistentVolumeClaim": {
                            "claimName": self.config.source_pvc,
                        },
                    },
                    {
                        "name": "artifacts",
                        "persistentVolumeClaim": {
                            "claimName": self.config.artifact_pvc,
                        },
                    },
                    {"name": "temporary", "emptyDir": {}},
                ],
            },
        }
        return {
            "apiVersion": "v1",
            "kind": "List",
            "items": [
                self._deny_network_policy(
                    name=self._network_policy_name(f"{job_id}-source"),
                    namespace=self.config.namespace,
                    labels=source_labels,
                ),
                pod,
            ],
        }

    def _artifact_pod_manifest(
        self,
        job_id: str,
        attempt: int = 1,
    ) -> dict[str, Any]:
        if (
            self.config.artifact_pvc is None
            or self.config.source_transport_image is None
        ):
            raise ValueError("Kubernetes artifact transport is not configured")
        pod_name = self._job_name(f"{job_id}-artifact-{attempt}")
        artifact_labels = {
            "app.kubernetes.io/managed-by": "paperforge",
            "paperforge-artifact-job": self._attempt_remote_name(
                job_id,
                attempt,
            ),
        }
        pod = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": pod_name,
                "namespace": self.config.namespace,
                "labels": artifact_labels,
            },
            "spec": {
                "restartPolicy": "Never",
                "automountServiceAccountToken": False,
                "securityContext": {
                    "runAsNonRoot": True,
                    "runAsUser": self.config.run_as_user,
                    "runAsGroup": self.config.run_as_group,
                    "fsGroup": self.config.fs_group,
                    "fsGroupChangePolicy": "OnRootMismatch",
                    "seccompProfile": {"type": "RuntimeDefault"},
                },
                "containers": [
                    {
                        "name": "artifact-transport",
                        "image": self.config.source_transport_image,
                        "imagePullPolicy": self.config.image_pull_policy,
                        "command": [
                            "sh",
                            "-c",
                            "trap : TERM INT; sleep 600 & wait",
                        ],
                        "securityContext": {
                            "allowPrivilegeEscalation": False,
                            "capabilities": {"drop": ["ALL"]},
                            "readOnlyRootFilesystem": True,
                            "runAsNonRoot": True,
                            "runAsUser": self.config.run_as_user,
                            "runAsGroup": self.config.run_as_group,
                            "seccompProfile": {"type": "RuntimeDefault"},
                        },
                        "volumeMounts": [
                            {
                                "name": "artifacts",
                                "mountPath": "/pvc-artifacts",
                            },
                            {
                                "name": "temporary",
                                "mountPath": "/tmp",
                            },
                        ],
                    }
                ],
                "volumes": [
                    {
                        "name": "artifacts",
                        "persistentVolumeClaim": {
                            "claimName": self.config.artifact_pvc,
                        },
                    },
                    {"name": "temporary", "emptyDir": {}},
                ],
            },
        }
        return {
            "apiVersion": "v1",
            "kind": "List",
            "items": [
                self._deny_network_policy(
                    name=self._network_policy_name(
                        f"{job_id}-artifact-{attempt}"
                    ),
                    namespace=self.config.namespace,
                    labels=artifact_labels,
                ),
                pod,
            ],
        }

    def stage_source(
        self,
        spec: JobSpec,
        source_snapshot: str | Path,
        *,
        execute: bool = False,
    ) -> JobResult:
        job_id = self._job_id(spec)
        expected_sha256 = str(
            spec.metadata.get("source_snapshot_sha256") or ""
        )
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise ValueError(
                "Kubernetes source staging requires a bound source sha256"
            )
        if (
            self.config.source_pvc is None
            or self.config.artifact_pvc is None
            or self.config.source_transport_image is None
        ):
            raise ValueError(
                "executable Kubernetes jobs require configured source transport"
            )
        try:
            bundle = create_verified_source_bundle(
                source_snapshot,
                canonical_worktree=spec.workdir,
                expected_source_sha256=expected_sha256,
                staging_dir=self._job_state_dir(job_id),
            )
        except SourceBundleError as exc:
            raise ValueError(str(exc)) from exc
        pod_name = self._job_name(f"{job_id}-source")
        source_path = f"/pvc-source/sources/{job_id}"
        source_manifest = self._write_payload(
            self._source_manifest_path(job_id),
            self._source_pod_manifest(job_id),
        )
        apply = self._kubectl("apply", "-f", str(source_manifest))
        wait = self._kubectl(
            "wait",
            "--for=condition=Ready",
            f"pod/{pod_name}",
            "--timeout=120s",
        )
        prepare_script = (
            "[ ! -L /pvc-source ] && mkdir -p /pvc-source/sources && "
            "[ ! -L /pvc-source/sources ] && "
            f"[ ! -L {shlex.quote(source_path)} ] && "
            f"rm -rf {shlex.quote(source_path)} && "
            f"mkdir -p {shlex.quote(source_path)}"
        )
        prepare = self._kubectl(
            "exec",
            pod_name,
            "--",
            "sh",
            "-c",
            prepare_script,
        )
        upload = self._kubectl(
            "cp",
            str(bundle.path),
            f"{self.config.namespace}/{pod_name}:/tmp/source.tar",
        )
        output_setup_parts: list[str] = []
        for index, path in enumerate(spec.outputs):
            source_output = PurePosixPath(source_path) / path
            mounted_output = _OUTPUT_MOUNT_ROOT / str(index)
            output_setup_parts.append(
                f"mkdir -p {shlex.quote(source_output.parent.as_posix())} && "
                f"rm -f {shlex.quote(source_output.as_posix())} && "
                f"ln -s {shlex.quote(mounted_output.as_posix())} "
                f"{shlex.quote(source_output.as_posix())}"
            )
        output_setup = " && ".join(output_setup_parts)
        verify_script = (
            f"printf '%s  %s\\n' {shlex.quote(bundle.archive_sha256)} "
            "'/tmp/source.tar' | sha256sum -c - && "
            f"tar -xf /tmp/source.tar -C {shlex.quote(source_path)} && "
            f"printf '%s\\n' {shlex.quote(expected_sha256)} > "
            f"{shlex.quote(source_path + '/.paperforge-source-sha256')} && "
            f"chmod -R u+rwX,go-rwx {shlex.quote(source_path)}"
        )
        if output_setup:
            verify_script += f" && {output_setup}"
        verify = self._kubectl(
            "exec",
            pod_name,
            "--",
            "sh",
            "-c",
            verify_script,
        )
        delete = self._kubectl(
            "delete",
            f"pod/{pod_name}",
            f"networkpolicy/{self._network_policy_name(f'{job_id}-source')}",
            "--ignore-not-found=true",
            "--wait=true",
        )
        plan = self._plan(
            job_id=job_id,
            action="source-stage",
            argv=apply,
            description=f"stage immutable Kubernetes source for {job_id}",
            metadata={
                "commands": [
                    list(apply),
                    list(wait),
                    list(prepare),
                    list(upload),
                    list(verify),
                    list(delete),
                ],
                "source_sha256": expected_sha256,
                "archive_sha256": bundle.archive_sha256,
                "file_count": bundle.file_count,
            },
        )
        if not execute:
            bundle.path.unlink(missing_ok=True)
            return plan
        stdout: list[str] = []
        stderr: list[str] = []
        return_code = 0
        try:
            for command in (apply, wait, prepare, upload, verify):
                outcome = self._run(spec, command, timeout=300)
                stdout.append(outcome.stdout)
                stderr.append(outcome.stderr)
                if outcome.return_code != 0:
                    return_code = outcome.return_code
                    break
        finally:
            cleanup = self._run(spec, delete, timeout=120)
            stdout.append(cleanup.stdout)
            stderr.append(cleanup.stderr)
            bundle.path.unlink(missing_ok=True)
        return JobResult(
            job_id=job_id,
            backend=self.name,
            status=(
                JobStatus.SUCCEEDED
                if return_code == 0
                else JobStatus.FAILED
            ),
            executed=True,
            plan=plan.plan,
            return_code=return_code,
            stdout="".join(stdout),
            stderr="".join(stderr),
            message=(
                "Kubernetes source staging completed"
                if return_code == 0
                else "Kubernetes source staging failed"
            ),
            metadata={
                "source_sha256": expected_sha256,
                "archive_sha256": bundle.archive_sha256,
            },
        )

    def submit(self, spec: JobSpec, *, execute: bool | None = None) -> JobResult:
        job_id = self._job_id(spec)
        attempt = 1
        remote_name = self._attempt_remote_name(job_id, attempt)
        policy_apply, policy_verify, job_apply = self._execution_apply_commands(
            job_id,
            attempt,
        )
        manifest = self._manifest(spec, job_id, attempt)
        plan = self._plan(
            job_id=job_id,
            action="submit",
            argv=policy_apply,
            description=f"apply Kubernetes Job {remote_name}",
            environment_keys=tuple(spec.env),
            metadata={
                "remote_name": remote_name,
                "manifest": manifest,
                "commands": [
                    list(policy_apply),
                    list(policy_verify),
                    list(job_apply),
                ],
            },
            sensitive_values=self._sensitive_environment_values(spec.env),
        )
        self._remote_names[job_id] = remote_name
        self._remember(job_id, spec=spec, result=plan)
        if not self._should_execute(spec, execute):
            return plan
        self._reject_sensitive_remote_environment(spec)
        for command in (policy_apply, policy_verify, job_apply):
            self.policy.validate_command(command, self.policy_action)
        self._write_execution_manifests(spec, job_id, attempt)
        self._persist_submission_intent(spec, plan)
        policy_outcome = self._run(spec, policy_apply, timeout=60)
        verification = (
            self._run(spec, policy_verify, timeout=60)
            if policy_outcome.return_code == 0
            else policy_outcome
        )
        policy_ready = (
            policy_outcome.return_code == 0
            and verification.return_code == 0
            and bool(verification.stdout.strip())
        )
        outcome = (
            self._run(spec, job_apply, timeout=60)
            if policy_ready
            else verification
        )
        status = (
            JobStatus.SUBMITTED
            if policy_ready and outcome.return_code == 0
            else JobStatus.FAILED
        )
        result = JobResult(
            job_id=job_id,
            backend=self.name,
            status=status,
            executed=True,
            plan=plan.plan,
            return_code=outcome.return_code,
            stdout=(
                policy_outcome.stdout
                + (verification.stdout if verification is not policy_outcome else "")
                + (outcome.stdout if outcome is not verification else "")
            ),
            stderr=(
                policy_outcome.stderr
                + (verification.stderr if verification is not policy_outcome else "")
                + (outcome.stderr if outcome is not verification else "")
            ),
            message=(
                "Kubernetes job submitted"
                if outcome.return_code == 0
                else "Kubernetes submit failed"
            ),
            metadata={
                "remote_name": remote_name,
                "namespace": self.config.namespace,
                "manifest_path": str(self._manifest_path(job_id, attempt)),
                "attempt": attempt,
            },
            created_at=plan.created_at,
        )
        try:
            self._remember(job_id, result=result)
        except Exception:
            if status is JobStatus.SUBMITTED:
                self._run(
                    spec,
                    self._kubectl(
                        "delete",
                        f"job/{remote_name}",
                        "networkpolicy/"
                        f"{self._network_policy_name(f'{job_id}-attempt-{attempt}')}",
                        "--ignore-not-found=true",
                        "--wait=true",
                    ),
                    timeout=120,
                )
            raise
        return result

    def _remote_name(self, job_id: str) -> str:
        if job_id in self._remote_names:
            return self._remote_names[job_id]
        try:
            result = self._known_result(job_id)
            name = result.metadata.get("remote_name")
            if isinstance(name, str) and name:
                return name
            attempt = int(result.metadata.get("attempt") or 1)
        except (KeyError, TypeError, ValueError):
            attempt = 1
        return self._attempt_remote_name(job_id, attempt)

    @staticmethod
    def _status_from_job(payload: dict[str, Any]) -> JobStatus:
        status = payload.get("status") or {}
        conditions = status.get("conditions") or []
        for condition in conditions:
            if condition.get("status") != "True":
                continue
            if condition.get("type") == "Complete":
                return JobStatus.SUCCEEDED
            if condition.get("type") in {"Failed", "FailureTarget"}:
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
        previous = self._known_result(job_id)
        attempt = int(previous.metadata.get("attempt") or 1)
        argv = self._kubectl(
            "delete",
            f"job/{remote_name}",
            "networkpolicy/"
            f"{self._network_policy_name(f'{job_id}-attempt-{attempt}')}",
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
            metadata=previous.metadata,
            created_at=previous.created_at,
        )
        self._remember(job_id, result=result)
        return result

    def resume(self, job_id: str, *, execute: bool = False) -> JobResult:
        spec = self._known_spec(job_id)
        previous = self._known_result(job_id)
        previous_attempt = int(previous.metadata.get("attempt") or 1)
        previous_name = self._remote_name(job_id)
        attempt = previous_attempt + 1
        remote_name = self._attempt_remote_name(job_id, attempt)
        delete_argv = self._kubectl(
            "delete",
            f"job/{previous_name}",
            "networkpolicy/"
            f"{self._network_policy_name(f'{job_id}-attempt-{previous_attempt}')}",
            "--ignore-not-found=true",
            "--wait=true",
        )
        policy_apply, policy_verify, job_apply = self._execution_apply_commands(
            job_id,
            attempt,
        )
        plan = self._plan(
            job_id=job_id,
            action="resume",
            argv=policy_apply,
            description=f"recreate Kubernetes Job {remote_name}",
            metadata={
                "commands": [
                    list(delete_argv),
                    list(policy_apply),
                    list(policy_verify),
                    list(job_apply),
                ],
                "manifest": self._manifest(spec, job_id, attempt),
            },
            sensitive_values=self._sensitive_environment_values(spec.env),
        )
        if not execute:
            return plan
        current = self.status(job_id, execute=True)
        if current.status in {
            JobStatus.SUBMITTED,
            JobStatus.QUEUED,
            JobStatus.RUNNING,
            JobStatus.SUSPENDED,
        }:
            raise RuntimeError(f"cannot resume active Kubernetes job {job_id}")
        self._reject_sensitive_remote_environment(spec)
        for command in (policy_apply, policy_verify, job_apply):
            self.policy.validate_command(command, self.policy_action)
        self._write_execution_manifests(spec, job_id, attempt)
        deleted = self._run(spec, delete_argv, timeout=120)
        policy_outcome = (
            self._run(spec, policy_apply, timeout=60)
            if deleted.return_code == 0
            else deleted
        )
        verification = (
            self._run(spec, policy_verify, timeout=60)
            if policy_outcome.return_code == 0
            else policy_outcome
        )
        policy_ready = (
            deleted.return_code == 0
            and policy_outcome.return_code == 0
            and verification.return_code == 0
            and bool(verification.stdout.strip())
        )
        outcome = (
            self._run(spec, job_apply, timeout=60)
            if policy_ready
            else verification
        )
        status = (
            JobStatus.SUBMITTED
            if policy_ready and outcome.return_code == 0
            else JobStatus.FAILED
        )
        result = JobResult(
            job_id=job_id,
            backend=self.name,
            status=status,
            executed=True,
            plan=plan.plan,
            return_code=outcome.return_code,
            stdout=(
                deleted.stdout
                + (policy_outcome.stdout if policy_outcome is not deleted else "")
                + (verification.stdout if verification is not policy_outcome else "")
                + (outcome.stdout if outcome is not verification else "")
            ),
            stderr=(
                deleted.stderr
                + (policy_outcome.stderr if policy_outcome is not deleted else "")
                + (verification.stderr if verification is not policy_outcome else "")
                + (outcome.stderr if outcome is not verification else "")
            ),
            message=(
                "Kubernetes job recreated"
                if status is JobStatus.SUBMITTED
                else "Kubernetes resume failed"
            ),
            metadata={
                "remote_name": remote_name,
                "namespace": self.config.namespace,
                "manifest_path": str(self._manifest_path(job_id, attempt)),
                "attempt": attempt,
            },
            created_at=previous.created_at,
        )
        self._remote_names[job_id] = remote_name
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

    def _sync_executable_artifacts(
        self,
        *,
        spec: JobSpec,
        job_id: str,
        local_root: Path,
        selected: tuple[str, ...],
        direction: ArtifactDirection,
        execute: bool,
    ) -> JobResult:
        previous = self._known_result(job_id)
        raw_attempt = previous.metadata.get("attempt")
        attempt = (
            int(raw_attempt)
            if isinstance(raw_attempt, int) and not isinstance(raw_attempt, bool)
            else 1
        )
        pod_name = self._job_name(f"{job_id}-artifact-{attempt}")
        policy_name = self._network_policy_name(
            f"{job_id}-artifact-{attempt}"
        )
        manifest_path = self._artifact_manifest_path(job_id, attempt)
        apply = self._kubectl("apply", "-f", str(manifest_path))
        wait = self._kubectl(
            "wait",
            "--for=condition=Ready",
            f"pod/{pod_name}",
            "--timeout=120s",
        )
        delete = self._kubectl(
            "delete",
            f"pod/{pod_name}",
            f"networkpolicy/{policy_name}",
            "--ignore-not-found=true",
            "--wait=true",
        )
        staging_root = self._job_state_path(
            job_id,
            f"attempts/{attempt}/artifact-download",
        )
        if direction is ArtifactDirection.DOWNLOAD:
            for pattern in selected:
                safe_artifact_destination(local_root, pattern)
        else:
            local_root = safe_artifact_root(local_root, create=False)
            for pattern in selected:
                safe_artifact_file(
                    local_root,
                    pattern,
                    require_exists=True,
                )
        copies: list[tuple[str, ...]] = []
        for pattern in selected:
            local_target = (
                staging_root / pattern
                if direction is ArtifactDirection.DOWNLOAD
                else local_root / pattern
            )
            remote = (
                PurePosixPath("/pvc-artifacts")
                / "artifacts"
                / job_id
                / "attempts"
                / str(attempt)
                / pattern
            ).as_posix()
            if direction is ArtifactDirection.DOWNLOAD:
                copies.append(
                    self._kubectl(
                        "cp",
                        f"{self.config.namespace}/{pod_name}:{remote}",
                        str(local_target),
                    )
                )
            else:
                copies.append(
                    self._kubectl(
                        "cp",
                        str(local_target),
                        f"{self.config.namespace}/{pod_name}:{remote}",
                    )
                )
        plan = self._plan(
            job_id=job_id,
            action="artifact-sync",
            argv=apply,
            description=f"{direction.value} Kubernetes artifacts for {job_id}",
            metadata={
                "commands": [
                    list(apply),
                    list(wait),
                    *(list(command) for command in copies),
                    list(delete),
                ],
                "paths": list(selected),
                "direction": direction.value,
                "transport_pod": pod_name,
            },
        )
        if not execute:
            return plan
        self.policy.require(
            self.policy_action,
            detail=f"Kubernetes artifact sync {job_id}",
        )
        if direction is ArtifactDirection.DOWNLOAD:
            if staging_root.exists():
                safe_artifact_root(staging_root, create=False)
                shutil.rmtree(staging_root)
            safe_artifact_root(staging_root, create=True)
            for pattern in selected:
                safe_artifact_file(
                    staging_root,
                    pattern,
                    require_exists=False,
                )
        self._write_payload(
            manifest_path,
            self._artifact_pod_manifest(job_id, attempt),
        )
        stdout: list[str] = []
        stderr: list[str] = []
        return_code = 0
        records: tuple[ArtifactRecord, ...] = ()
        try:
            try:
                for command in (apply, wait, *copies):
                    outcome = self._run(spec, command, timeout=300)
                    stdout.append(outcome.stdout)
                    stderr.append(outcome.stderr)
                    if outcome.return_code != 0:
                        return_code = outcome.return_code
                        break
            finally:
                cleanup = self._run(spec, delete, timeout=120)
                stdout.append(cleanup.stdout)
                stderr.append(cleanup.stderr)
                if return_code == 0 and cleanup.return_code != 0:
                    return_code = cleanup.return_code
            if return_code == 0 and direction is ArtifactDirection.DOWNLOAD:
                try:
                    records = copy_local_artifacts(
                        source_root=staging_root,
                        destination_root=local_root,
                        patterns=selected,
                        attempt_id=attempt,
                    )
                except (OSError, ValueError) as exc:
                    return_code = 1
                    stderr.append(f"{exc}\n")
        finally:
            if staging_root.exists():
                safe_artifact_root(staging_root, create=False)
                shutil.rmtree(staging_root)
        return JobResult(
            job_id=job_id,
            backend=self.name,
            status=(
                JobStatus.SUCCEEDED
                if return_code == 0
                else JobStatus.FAILED
            ),
            executed=True,
            plan=plan.plan,
            return_code=return_code,
            stdout="".join(stdout),
            stderr="".join(stderr),
            artifacts=records,
            message=(
                "Kubernetes artifact sync completed"
                if return_code == 0
                else "Kubernetes artifact sync failed"
            ),
            metadata={
                "pod": pod_name,
                "paths": list(selected),
                "direction": direction.value,
                "attempt": attempt,
            },
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
        local_root = Path(local_path).expanduser().absolute()
        if spec.execute:
            return self._sync_executable_artifacts(
                spec=spec,
                job_id=job_id,
                local_root=local_root,
                selected=selected,
                direction=direction,
                execute=execute,
            )
        remote_name = self._remote_name(job_id)
        workdir = PurePosixPath(
            str(spec.backend_options.get("container_workdir", self.config.container_workdir))
        )
        staging_root = self._job_state_path(
            job_id,
            "attempts/1/legacy-artifact-download",
        )
        if direction is ArtifactDirection.DOWNLOAD:
            for pattern in selected:
                safe_artifact_destination(local_root, pattern)
        else:
            local_root = safe_artifact_root(local_root, create=False)
            for pattern in selected:
                safe_artifact_file(local_root, pattern, require_exists=True)
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
        if direction is ArtifactDirection.DOWNLOAD:
            if staging_root.exists():
                safe_artifact_root(staging_root, create=False)
                shutil.rmtree(staging_root)
            safe_artifact_root(staging_root, create=True)
            for pattern in selected:
                safe_artifact_file(
                    staging_root,
                    pattern,
                    require_exists=False,
                )
        stdout: list[str] = []
        stderr: list[str] = []
        return_code = 0
        records: tuple[ArtifactRecord, ...] = ()
        try:
            for pattern in selected:
                remote = (workdir / pattern).as_posix()
                local_target = (
                    staging_root / pattern
                    if direction is ArtifactDirection.DOWNLOAD
                    else local_root / pattern
                )
                if direction is ArtifactDirection.DOWNLOAD:
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
                outcome = self._run(
                    spec,
                    argv,
                    timeout=spec.resources.timeout_seconds,
                )
                stdout.append(outcome.stdout)
                stderr.append(outcome.stderr)
                if outcome.return_code != 0:
                    return_code = outcome.return_code
                    break
            if return_code == 0 and direction is ArtifactDirection.DOWNLOAD:
                records = copy_local_artifacts(
                    source_root=staging_root,
                    destination_root=local_root,
                    patterns=selected,
                    attempt_id=1,
                )
            elif return_code == 0:
                records = tuple(
                    file_record(
                        safe_artifact_file(
                            local_root,
                            pattern,
                            require_exists=True,
                        ),
                        display_path=pattern,
                        attempt_id=1,
                    )
                    for pattern in selected
                )
        except (OSError, ValueError) as exc:
            return_code = 1
            stderr.append(str(exc))
        finally:
            if (
                direction is ArtifactDirection.DOWNLOAD
                and staging_root.exists()
            ):
                safe_artifact_root(staging_root, create=False)
                shutil.rmtree(staging_root)
        return JobResult(
            job_id=job_id,
            backend=self.name,
            status=JobStatus.SUCCEEDED if return_code == 0 else JobStatus.FAILED,
            executed=True,
            plan=plan.plan,
            return_code=return_code,
            stdout="".join(stdout),
            stderr="".join(stderr),
            artifacts=records,
            message=(
                "Kubernetes artifact sync completed"
                if return_code == 0
                else "Kubernetes artifact sync failed"
            ),
            metadata={
                "pod": pod,
                "paths": list(selected),
                "direction": direction.value,
                "attempt": 1,
            },
        )


KubernetesComputeBackend = KubernetesBackend
