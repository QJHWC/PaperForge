from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from paperforge.compute import (
    ArtifactDirection,
    CloudSSHBackend,
    CloudSSHConfig,
    ComputeBackend,
    DockerBackend,
    DockerConfig,
    JobSpec,
    JobStatus,
    KubernetesBackend,
    KubernetesConfig,
    LocalBackend,
    ResourceSpec,
    SensitiveEnvironmentError,
    SlurmBackend,
    SlurmConfig,
    SSHBackend,
    SSHConfig,
    SSHSecurityError,
)
from paperforge.compute.base import CommandOutcome
from paperforge.compute.ssh import _validate_windows_acl_payload
from paperforge.models import ExecutionProfile
from paperforge.policy import ExecutionPolicy, PolicyViolation


def _secure_test_file(path: Path, *, mode: int) -> None:
    if os.name != "nt":
        path.chmod(mode)
        return
    username = os.environ.get("USERNAME", "").strip()
    if not username:
        raise RuntimeError("Windows test user is unavailable")
    subprocess.run(
        [
            "icacls",
            str(path),
            "/inheritance:r",
            "/grant:r",
            f"{username}:(F)",
            "*S-1-5-18:(F)",
            "*S-1-5-32-544:(F)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )


class _SlurmWithoutAccountingRunner:
    def __init__(self, *, squeue_return_code: int = 0, missing_sacct: bool = False) -> None:
        self.squeue_return_code = squeue_return_code
        self.missing_sacct = missing_sacct

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int | None = None,
    ) -> CommandOutcome:
        del cwd, env, timeout
        command = tuple(argv)
        if command[0] == "squeue":
            return CommandOutcome(self.squeue_return_code, "", "queue unavailable\n")
        if command[0] == "sacct":
            if self.missing_sacct:
                raise FileNotFoundError("sacct")
            return CommandOutcome(1, "", "Slurm accounting storage is disabled\n")
        if command[:3] == ("scontrol", "show", "job"):
            return CommandOutcome(
                0,
                "JobId=42 JobState=COMPLETED ExitCode=0:0\n",
                "",
            )
        raise AssertionError(f"unexpected command: {command}")


def _ssh_config(tmp_path: Path) -> SSHConfig:
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text(
        "compute.example ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITest\n",
        encoding="utf-8",
    )
    _secure_test_file(known_hosts, mode=0o644)
    identity = tmp_path / "id_ed25519"
    identity.write_text("test-key-material", encoding="utf-8")
    _secure_test_file(identity, mode=0o600)
    return SSHConfig(
        host="compute.example",
        user="paperforge",
        identity_file=identity,
        known_hosts_file=known_hosts,
    )


def _all_backends(tmp_path: Path) -> list[ComputeBackend]:
    ssh = _ssh_config(tmp_path)
    return [
        LocalBackend(state_dir=tmp_path / "local-state"),
        DockerBackend(
            DockerConfig(image="python:3.12-slim"),
            state_dir=tmp_path / "docker-state",
        ),
        SSHBackend(ssh, state_dir=tmp_path / "ssh-state"),
        SlurmBackend(SlurmConfig(), state_dir=tmp_path / "slurm-state"),
        KubernetesBackend(
            KubernetesConfig(image="python:3.12-slim", namespace="research"),
            state_dir=tmp_path / "k8s-state",
        ),
        CloudSSHBackend(
            CloudSSHConfig(
                ssh=ssh,
                provider="aws",
                instance_id="i-testfixture",
                region="us-test-1",
            ),
            state_dir=tmp_path / "cloud-state",
        ),
    ]


def test_job_spec_is_normalized_serializable_and_rejects_unsafe_outputs() -> None:
    spec = JobSpec(
        name="metric-eval",
        command=[sys.executable, "-c", "print('ok')"],
        env={"RUN_KIND": "evaluation"},
        outputs=["metrics.json"],
        resources=ResourceSpec(cpus=2, memory_mb=1024, gpus=0),
    )

    assert spec.command == (sys.executable, "-c", "print('ok')")
    assert spec.outputs == ("metrics.json",)
    assert spec.to_dict()["resources"]["cpus"] == 2
    assert not spec.execute

    with pytest.raises(ValueError, match="relative"):
        JobSpec(name="unsafe", command=["true"], outputs=["../secret"])
    with pytest.raises(ValueError, match="broad"):
        JobSpec(name="unsafe-root", command=["true"], outputs=["."])
    with pytest.raises(ValueError, match="broad"):
        JobSpec(name="unsafe-glob", command=["true"], outputs=["**/*"])

    mapped = JobSpec(
        name="mapped-resources",
        command=["true"],
        resources={"cpus": 3, "memory_mb": 2048},
    )
    assert mapped.resources == ResourceSpec(cpus=3, memory_mb=2048)
    assert mapped.dry_run

    secret_spec = JobSpec(
        name="safe-serialization",
        command=["true"],
        env={"API_TOKEN": "do-not-serialize", "RUN_KIND": "evaluation"},
    )
    assert secret_spec.to_dict()["env"] == {
        "API_TOKEN": "***",
        "RUN_KIND": "evaluation",
    }


def test_every_backend_plans_all_lifecycle_operations_by_default(
    tmp_path: Path,
) -> None:
    spec = JobSpec(
        name="dry-run",
        command=["python", "-c", "print('never executed')"],
        outputs=["metrics.json"],
    )

    for backend in _all_backends(tmp_path):
        submitted = backend.submit(spec)
        assert submitted.status is JobStatus.PLANNED
        assert not submitted.executed
        assert submitted.plan is not None

        for result in (
            backend.status(submitted.job_id),
            backend.cancel(submitted.job_id),
            backend.resume(submitted.job_id),
            backend.logs(submitted.job_id),
            backend.sync_artifacts(
                submitted.job_id,
                tmp_path / "synced" / backend.name,
                direction=ArtifactDirection.DOWNLOAD,
            ),
        ):
            assert not result.executed
            assert result.plan is not None


@pytest.mark.parametrize(
    ("squeue_return_code", "missing_sacct"),
    [(0, False), (1, False), (0, True)],
)
def test_slurm_status_falls_back_when_accounting_is_disabled(
    tmp_path: Path,
    squeue_return_code: int,
    missing_sacct: bool,
) -> None:
    backend = SlurmBackend(
        SlurmConfig(),
        runner=_SlurmWithoutAccountingRunner(
            squeue_return_code=squeue_return_code,
            missing_sacct=missing_sacct,
        ),
        policy=ExecutionPolicy(ExecutionProfile.FULL),
        state_dir=tmp_path / "state",
    )
    backend.submit(
        JobSpec(name="slurm-fallback", job_id="42", command=["true"]),
    )

    result = backend.status("42", execute=True)

    assert result.status is JobStatus.SUCCEEDED
    assert result.stdout == "COMPLETED"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("PENDING", JobStatus.QUEUED),
        ("REQUEUED", JobStatus.QUEUED),
        ("REQUEUE_HOLD", JobStatus.SUSPENDED),
        ("RUNNING", JobStatus.RUNNING),
        ("STAGE_OUT", JobStatus.RUNNING),
        ("STOPPED", JobStatus.SUSPENDED),
        ("COMPLETED", JobStatus.SUCCEEDED),
        ("BOOT_FAIL", JobStatus.FAILED),
        ("DEADLINE", JobStatus.FAILED),
        ("LAUNCH_FAILED", JobStatus.FAILED),
        ("REVOKED", JobStatus.CANCELLED),
        ("CANCELLED+", JobStatus.CANCELLED),
    ],
)
def test_slurm_maps_documented_scheduler_states(raw: str, expected: JobStatus) -> None:
    assert SlurmBackend._map_status(raw) is expected


def test_windows_acl_allows_read_only_known_hosts_access() -> None:
    payload = {
        "dacl_present": True,
        "dacl_null": False,
        "current": "S-1-5-21-1000",
        "owner": "S-1-5-21-1000",
        "rules": [
            {
                "sid": "S-1-5-21-1000",
                "rights": 2032127,
                "type": "Allow",
                "propagation": "None",
            },
            {
                "sid": "S-1-5-32-545",
                "rights": 131209,
                "type": "Allow",
                "propagation": "None",
            },
        ],
    }

    _validate_windows_acl_payload(payload, label="known_hosts", private=False)


@pytest.mark.parametrize("private", [False, True])
def test_windows_acl_rejects_untrusted_writers(private: bool) -> None:
    payload = {
        "dacl_present": True,
        "dacl_null": False,
        "current": "S-1-5-21-1000",
        "owner": "S-1-5-21-1000",
        "rules": [
            {
                "sid": "S-1-5-32-545",
                "rights": 278,
                "type": "Allow",
                "propagation": "None",
            }
        ],
    }

    with pytest.raises(SSHSecurityError, match="untrusted principal"):
        _validate_windows_acl_payload(
            payload,
            label="identity file" if private else "known_hosts",
            private=private,
        )


def test_windows_acl_rejects_untrusted_private_key_readers() -> None:
    payload = {
        "dacl_present": True,
        "dacl_null": False,
        "current": "S-1-5-21-1000",
        "owner": "S-1-5-21-1000",
        "rules": [
            {
                "sid": "S-1-5-32-545",
                "rights": 131209,
                "type": "Allow",
                "propagation": "None",
            }
        ],
    }

    with pytest.raises(SSHSecurityError, match="untrusted principal"):
        _validate_windows_acl_payload(
            payload,
            label="identity file",
            private=True,
        )


@pytest.mark.parametrize(
    ("dacl_present", "dacl_null"),
    [
        (False, False),
        (True, True),
    ],
)
def test_windows_acl_rejects_missing_or_null_dacl(
    dacl_present: bool,
    dacl_null: bool,
) -> None:
    payload = {
        "dacl_present": dacl_present,
        "dacl_null": dacl_null,
        "current": "S-1-5-21-1000",
        "owner": "S-1-5-21-1000",
        "rules": [],
    }
    with pytest.raises(SSHSecurityError, match="unsafe Windows DACL"):
        _validate_windows_acl_payload(
            payload,
            label="identity file",
            private=True,
        )


def test_dry_run_plans_do_not_expose_environment_values(tmp_path: Path) -> None:
    spec = JobSpec(
        name="redacted-plan",
        command=["python", "-c", "print('planned')"],
        env={"API_TOKEN": "sensitive-fixture-value"},
        outputs=["metrics.json"],
    )

    for backend in _all_backends(tmp_path):
        serialized = json.dumps(backend.submit(spec).to_dict(), sort_keys=True)
        assert "sensitive-fixture-value" not in serialized
        assert "API_TOKEN" in serialized


def test_remote_execution_rejects_sensitive_environment_values(tmp_path: Path) -> None:
    spec = JobSpec(
        name="remote-secret",
        command=["true"],
        env={"API_TOKEN": "sensitive-fixture-value"},
    )

    for backend in _all_backends(tmp_path)[1:]:
        with pytest.raises(SensitiveEnvironmentError, match="API_TOKEN"):
            backend.submit(spec, execute=True)


def test_local_backend_executes_only_when_explicit_and_syncs_real_artifacts(
    tmp_path: Path,
) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    code = (
        "from pathlib import Path; "
        "Path('metrics.json').write_text('{\"accuracy\": 0.75}'); "
        "print('measured accuracy=0.75')"
    )
    backend = LocalBackend(
        policy=ExecutionPolicy(ExecutionProfile.FULL),
        state_dir=tmp_path / "state",
    )
    result = backend.submit(
        JobSpec(
            name="local-real",
            command=[sys.executable, "-c", code],
            workdir=workdir,
            outputs=["metrics.json"],
        ),
        execute=True,
    )

    deadline = time.monotonic() + 5
    current = result
    while current.status in {JobStatus.SUBMITTED, JobStatus.RUNNING}:
        assert time.monotonic() < deadline
        time.sleep(0.02)
        current = backend.status(result.job_id, execute=True)

    assert current.status is JobStatus.SUCCEEDED
    log_result = backend.logs(result.job_id, execute=True)
    assert "measured accuracy=0.75" in log_result.stdout

    destination = tmp_path / "download"
    synced = backend.sync_artifacts(
        result.job_id,
        destination,
        direction=ArtifactDirection.DOWNLOAD,
        execute=True,
    )
    assert synced.status is JobStatus.SUCCEEDED
    assert (destination / "metrics.json").read_text(encoding="utf-8") == ('{"accuracy": 0.75}')


def test_local_backend_redacts_logs_and_recovers_completed_state(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    secret = "compute-secret-canary"
    backend = LocalBackend(
        policy=ExecutionPolicy(ExecutionProfile.FULL),
        state_dir=state_dir,
    )
    submitted = backend.submit(
        JobSpec(
            name="redacted-and-durable",
            command=[
                sys.executable,
                "-c",
                "import os; print(os.environ['API_TOKEN'])",
            ],
            env={"API_TOKEN": secret},
        ),
        execute=True,
    )
    current = submitted
    deadline = time.monotonic() + 5
    while current.status in {JobStatus.SUBMITTED, JobStatus.RUNNING}:
        assert time.monotonic() < deadline
        time.sleep(0.02)
        current = backend.status(submitted.job_id, execute=True)

    assert current.status is JobStatus.SUCCEEDED
    logs = backend.logs(submitted.job_id, execute=True)
    assert secret not in logs.stdout
    assert "***" in logs.stdout

    recovered = LocalBackend(
        policy=ExecutionPolicy(ExecutionProfile.FULL),
        state_dir=state_dir,
    ).status(submitted.job_id, execute=True)
    assert recovered.status is JobStatus.SUCCEEDED
    assert recovered.job_id == submitted.job_id


def test_execution_policy_is_checked_only_for_explicit_execution(
    tmp_path: Path,
) -> None:
    backend = LocalBackend(
        policy=ExecutionPolicy(ExecutionProfile.WRITING_ONLY),
        state_dir=tmp_path / "state",
    )
    spec = JobSpec(name="experiment", command=["python", "experiment.py"])

    assert backend.submit(spec).status is JobStatus.PLANNED
    with pytest.raises(PolicyViolation):
        backend.submit(spec, execute=True)


def test_remote_policy_denial_happens_before_manifest_side_effect(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    backend = KubernetesBackend(
        KubernetesConfig(image="python:3.12-slim"),
        policy=ExecutionPolicy(ExecutionProfile.WRITING_ONLY),
        state_dir=state_dir,
    )

    with pytest.raises(PolicyViolation):
        backend.submit(
            JobSpec(name="denied-remote", command=["python", "experiment.py"]),
            execute=True,
        )

    assert not state_dir.exists()


def test_local_artifact_sync_rejects_symlink_escape(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("must-not-sync", encoding="utf-8")
    (workdir / "artifact.txt").symlink_to(outside)
    backend = LocalBackend(
        policy=ExecutionPolicy(ExecutionProfile.FULL),
        state_dir=tmp_path / "state",
    )
    planned = backend.submit(
        JobSpec(
            name="symlink-artifact",
            command=["true"],
            workdir=workdir,
            outputs=["artifact.txt"],
        )
    )

    result = backend.sync_artifacts(
        planned.job_id,
        tmp_path / "download",
        execute=True,
    )

    assert result.status is JobStatus.FAILED
    assert "symbolic link" in result.stderr
    assert not (tmp_path / "download" / "artifact.txt").exists()


def test_artifact_override_cannot_expand_declared_allowlist(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "metrics.json").write_text("{}", encoding="utf-8")
    (workdir / "secret.txt").write_text("no", encoding="utf-8")
    backend = LocalBackend(
        policy=ExecutionPolicy(ExecutionProfile.FULL),
        state_dir=tmp_path / "state",
    )
    planned = backend.submit(
        JobSpec(
            name="allowlisted-artifact",
            command=["true"],
            workdir=workdir,
            outputs=["metrics.json"],
        )
    )

    with pytest.raises(ValueError, match="declared outputs"):
        backend.sync_artifacts(
            planned.job_id,
            tmp_path / "download",
            patterns=["secret.txt"],
        )


def test_backend_runtime_names_keep_digest_when_job_ids_are_truncated(
    tmp_path: Path,
) -> None:
    prefix = "same-prefix-" + "x" * 100
    first = JobSpec(
        name="first",
        job_id=f"{prefix}a",
        command=["true"],
    )
    second = JobSpec(
        name="second",
        job_id=f"{prefix}b",
        command=["true"],
    )
    docker = DockerBackend(
        DockerConfig(image="python:3.12-slim"),
        state_dir=tmp_path / "docker",
    )
    kubernetes = KubernetesBackend(
        KubernetesConfig(image="python:3.12-slim"),
        state_dir=tmp_path / "kubernetes",
    )

    assert docker.submit(first).metadata["container_name"] != (
        docker.submit(second).metadata["container_name"]
    )
    assert kubernetes.submit(first).metadata["remote_name"] != (
        kubernetes.submit(second).metadata["remote_name"]
    )


def test_ssh_contract_enforces_host_verification_and_private_identity(
    tmp_path: Path,
) -> None:
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("host ssh-ed25519 fixture\n", encoding="utf-8")
    _secure_test_file(known_hosts, mode=0o644)
    identity = tmp_path / "id_ed25519"
    identity.write_text("fixture", encoding="utf-8")

    if os.name != "nt":
        identity.chmod(0o644)
        with pytest.raises(SSHSecurityError, match="permissions"):
            SSHConfig(
                host="compute.example",
                user="paperforge",
                identity_file=identity,
                known_hosts_file=known_hosts,
            )

    _secure_test_file(identity, mode=0o600)
    with pytest.raises(SSHSecurityError, match="host key"):
        SSHConfig(
            host="compute.example",
            user="paperforge",
            identity_file=identity,
            known_hosts_file=known_hosts,
            strict_host_key_checking=False,
        )

    with pytest.raises(SSHSecurityError, match="root"):
        SSHConfig(
            host="compute.example",
            user="root",
            identity_file=identity,
            known_hosts_file=known_hosts,
        )

    config = SSHConfig(
        host="compute.example",
        user="paperforge",
        identity_file=identity,
        known_hosts_file=known_hosts,
    )
    argv = config.base_argv()
    assert "BatchMode=yes" in argv
    assert "StrictHostKeyChecking=yes" in argv
    assert "ForwardAgent=no" in argv
    assert "ClearAllForwardings=yes" in argv
    assert f"UserKnownHostsFile={known_hosts.resolve()}" in argv
    assert os.fspath(identity.resolve()) in argv


def test_ssh_upload_denylist_rejects_secret_artifacts(tmp_path: Path) -> None:
    backend = SSHBackend(
        _ssh_config(tmp_path),
        state_dir=tmp_path / "state",
    )
    result = backend.submit(
        JobSpec(
            name="unsafe-upload",
            command=["true"],
            outputs=["results.json", "client-secret.pem"],
        )
    )

    with pytest.raises(SSHSecurityError, match="denylist"):
        backend.sync_artifacts(
            result.job_id,
            tmp_path / "upload",
            direction=ArtifactDirection.UPLOAD,
        )


def test_kubernetes_artifact_plan_contains_no_placeholder(tmp_path: Path) -> None:
    backend = KubernetesBackend(
        KubernetesConfig(image="python:3.12-slim"),
        state_dir=tmp_path / "state",
    )
    result = backend.submit(
        JobSpec(name="artifact-plan", command=["true"], outputs=["metrics.json"])
    )
    plan = backend.sync_artifacts(result.job_id, tmp_path / "download")
    serialized = json.dumps(plan.to_dict(), sort_keys=True)

    assert "<pod-for-" not in serialized
    assert "jsonpath={.items[0].metadata.name}" in serialized
