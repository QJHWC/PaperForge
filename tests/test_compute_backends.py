from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
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
    SlurmBackend,
    SlurmConfig,
    SSHBackend,
    SSHConfig,
    SSHSecurityError,
)
from paperforge.compute.base import CommandOutcome
from paperforge.compute.source_bundle import (
    SourceBundleError,
    create_verified_source_bundle,
)
from paperforge.compute.ssh import _validate_windows_acl_payload
from paperforge.models import ExecutionProfile
from paperforge.path_safety import UnsafePathError
from paperforge.policy import ExecutionPolicy, PolicyViolation


def _local_test_timeout(seconds: int = 5) -> int:
    return 60 if os.name == "nt" else seconds


def _local_process_timeout(seconds: int = 5) -> int:
    return 30 if os.name == "nt" else seconds


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


class _DockerDeadlineRunner:
    container_id = "d" * 64

    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.removed = False

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
        self.commands.append(command)
        if command[1] == "run":
            return CommandOutcome(0, self.container_id + "\n", "")
        if command[1:3] == ("inspect", "--format"):
            if self.removed:
                return CommandOutcome(1, "", "No such container\n")
            if command[3] == "{{.Id}}":
                return CommandOutcome(0, self.container_id + "\n", "")
            return CommandOutcome(
                0,
                f"{self.container_id}|running|0\n",
                "",
            )
        if command[1:3] == ("rm", "--force"):
            self.removed = True
            return CommandOutcome(0, command[-1] + "\n", "")
        raise AssertionError(f"unexpected Docker command: {command}")


class _SSHIdentityRunner:
    container_id = "e" * 64

    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []

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
        self.commands.append(command)
        if "/proc/999/stat" in command[-1]:
            return CommandOutcome(3, "UNKNOWN\n", "")
        return CommandOutcome(0, f"123|456|{self.container_id}\n", "")


class _SSHTimeoutCleanupRunner:
    container_id = "f" * 64

    def __init__(self, *, cwd: Path, env: Mapping[str, str]) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.cwd = cwd
        self.env = dict(env)

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
        self.commands.append(command)
        script = command[-1]
        if "TIMED_OUT|124" not in script:
            return CommandOutcome(0, f"123|456|{self.container_id}\n", "")
        completed = subprocess.run(
            ["/bin/sh", "-c", script],
            cwd=self.cwd,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )
        return CommandOutcome(
            completed.returncode,
            completed.stdout,
            completed.stderr,
        )


class _SSHLaunchTimeoutRunner:
    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int | None = None,
    ) -> CommandOutcome:
        del cwd, env
        command = tuple(argv)
        self.commands.append(command)
        if "printf 'CLEANED\\n'" in command[-1]:
            return CommandOutcome(0, "CLEANED\n", "")
        raise subprocess.TimeoutExpired(command, timeout or 60)


class _SSHUnreachableRunner:
    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int | None = None,
    ) -> CommandOutcome:
        del cwd, env
        command = tuple(argv)
        self.commands.append(command)
        raise subprocess.TimeoutExpired(command, timeout or 60)


class _KubernetesArtifactRunner:
    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []

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
        self.commands.append(command)
        if "jsonpath={.items[0].metadata.name}" in command:
            return CommandOutcome(0, "artifact-pod", "")
        if "cp" in command:
            destination = Path(command[-1])
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text("REMOTE_DATA", encoding="utf-8")
            return CommandOutcome(0, "", "")
        raise AssertionError(f"unexpected Kubernetes command: {command}")


class _SlurmSubmitRunner:
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
        if command[0].endswith("sbatch"):
            return CommandOutcome(0, "42\n", "")
        raise AssertionError(f"unexpected Slurm command: {command}")


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

    with pytest.raises(ValueError, match="environment must not contain credentials"):
        JobSpec(
            name="unsafe-environment",
            command=["true"],
            env={"API_TOKEN": "do-not-serialize"},
        )
    with pytest.raises(ValueError, match="environment must not contain credentials"):
        JobSpec(
            name="disguised-environment",
            command=["true"],
            env={"FOO": "sk-" + ("x" * 24)},
        )
    with pytest.raises(ValueError, match="command must not contain credentials"):
        JobSpec(
            name="unsafe-command",
            command=["tool", "--api-key", "secret-command-fixture"],
        )


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


def test_slurm_recovers_scheduler_id_after_crash_window(tmp_path: Path) -> None:
    class SubmitRunner:
        def run(self, argv, **kwargs) -> CommandOutcome:
            del kwargs
            assert tuple(argv)[0] == "sbatch"
            return CommandOutcome(0, "314\n", "")

    state_dir = tmp_path / "state"
    spec = JobSpec(
        name="recover-submit",
        job_id="stable-local-job",
        command=["true"],
    )
    submitting = SlurmBackend(
        SlurmConfig(),
        runner=SubmitRunner(),
        policy=ExecutionPolicy(ExecutionProfile.FULL),
        state_dir=state_dir,
    )
    remember = submitting._remember

    def crash_before_final_persistence(job_id, *, spec=None, result=None):
        if (
            result is not None
            and result.executed
            and result.status is JobStatus.SUBMITTED
            and result.metadata.get("submission_intent") is not True
        ):
            raise SystemExit("simulated process loss")
        remember(job_id, spec=spec, result=result)

    submitting._remember = crash_before_final_persistence  # type: ignore[method-assign]
    with pytest.raises(SystemExit, match="simulated process loss"):
        submitting.submit(spec, execute=True)

    expected_name, expected_comment = SlurmBackend._submission_identity(
        spec.job_id or ""
    )

    class RecoveryRunner:
        def run(self, argv, **kwargs) -> CommandOutcome:
            del kwargs
            command = tuple(str(part) for part in argv)
            if command[0] == "squeue" and any(
                part.startswith("--name=") for part in command
            ):
                return CommandOutcome(
                    0,
                    f"314|{expected_name}|{expected_comment}\n",
                    "",
                )
            if command[0] == "sacct" and any(
                part.startswith("--name=") for part in command
            ):
                return CommandOutcome(0, "", "")
            if command[0] == "squeue" and "--jobs" in command:
                assert "314" in command
                return CommandOutcome(0, "COMPLETED\n", "")
            raise AssertionError(f"unexpected command: {command}")

    recovered = SlurmBackend(
        SlurmConfig(),
        runner=RecoveryRunner(),
        policy=ExecutionPolicy(ExecutionProfile.FULL),
        state_dir=state_dir,
    )

    result = recovered.status(spec.job_id or "", execute=True)

    assert result.status is JobStatus.SUCCEEDED
    assert result.metadata["slurm_job_id"] == "314"
    assert result.metadata["submission_reconciled"] is True


def test_slurm_executable_attempt_does_not_modify_source_outputs(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    output = source / "result.json"
    output.write_text("valuable-existing-result", encoding="utf-8")
    image = tmp_path / "fixture.sif"
    image.write_bytes(b"immutable-image")
    backend = SlurmBackend(
        SlurmConfig(
            container_runtime="singularity",
            container_image=image,
        ),
        runner=_SlurmSubmitRunner(),
        policy=ExecutionPolicy(ExecutionProfile.FULL),
        state_dir=tmp_path / "state",
    )
    spec = JobSpec(
        name="immutable-slurm-source",
        job_id="immutable-slurm-source",
        command=["python", "run.py"],
        workdir=source,
        outputs=["result.json"],
        execute=True,
    )

    result = backend.submit(spec, execute=True)

    assert result.status is JobStatus.SUBMITTED
    assert output.read_text(encoding="utf-8") == "valuable-existing-result"
    plan = result.plan
    assert plan is not None
    wrapped = plan.argv[plan.argv.index("--wrap") + 1]
    assert str(source) not in wrapped
    assert "/attempts/1/workspace:/workspace:ro" in wrapped.replace("\\", "/")


def test_executable_docker_rejects_automatic_removal(tmp_path: Path) -> None:
    backend = DockerBackend(
        DockerConfig(
            image="fixture@sha256:" + ("d" * 64),
            workspace_mount=tmp_path,
            remove_on_exit=True,
        ),
        policy=ExecutionPolicy(ExecutionProfile.FULL),
        state_dir=tmp_path / "state",
    )

    with pytest.raises(RuntimeError, match="cannot use remove_on_exit"):
        backend.submit(
            JobSpec(
                name="durable-container",
                command=["true"],
                execute=True,
                job_id="durable-container",
            )
        )


def test_linux_local_command_uses_networkless_bubblewrap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bubblewrap = tmp_path / "bwrap"
    bubblewrap.write_text("fixture", encoding="utf-8")
    workdir = tmp_path / "work"
    workdir.mkdir()
    backend = LocalBackend(
        policy=ExecutionPolicy(ExecutionProfile.FULL),
        state_dir=tmp_path / "state",
    )
    monkeypatch.setattr("paperforge.compute.local.platform.system", lambda: "Linux")
    original_which = shutil.which
    monkeypatch.setattr(
        "paperforge.compute.local.shutil.which",
        lambda value: str(bubblewrap) if value == "bwrap" else original_which(value),
    )
    spec = JobSpec(
        name="linux-sandbox-plan",
        command=[sys.executable, "-c", "print('ok')"],
        workdir=workdir,
        outputs=["result.txt"],
    )

    command, _ = backend._sandboxed_command(
        spec,
        workdir=workdir,
        job_dir=backend._job_state_dir("linux-sandbox-plan"),
        environment=backend._clean_environment(spec),
        sandbox_nonce="a" * 48,
    )

    assert command[0] == str(bubblewrap)
    assert "--unshare-all" in command
    assert "--clearenv" in command
    assert "--bind" in command
    assert str(workdir / "result.txt") in command


def test_windows_local_command_uses_appcontainer_helper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = {}
    for name in ("powershell.exe", "icacls.exe"):
        path = tmp_path / name
        path.write_text("fixture", encoding="utf-8")
        tools[name] = str(path)
    executable = tmp_path / "python.exe"
    executable.write_text("fixture", encoding="utf-8")
    workdir = tmp_path / "work"
    workdir.mkdir()
    backend = LocalBackend(
        policy=ExecutionPolicy(ExecutionProfile.FULL),
        state_dir=tmp_path / "state",
    )
    monkeypatch.setattr("paperforge.compute.local.platform.system", lambda: "Windows")
    monkeypatch.setattr(
        "paperforge.compute.local.shutil.which",
        lambda value: tools.get(value),
    )
    spec = JobSpec(
        name="windows-sandbox-plan",
        command=[str(executable), "-c", "print('ok')"],
        workdir=workdir,
        outputs=["result.txt"],
    )
    job_dir = backend._job_state_dir("windows-sandbox-plan")

    command, _ = backend._sandboxed_command(
        spec,
        workdir=workdir,
        job_dir=job_dir,
        environment=backend._clean_environment(spec),
        sandbox_nonce="b" * 48,
    )

    assert command[1:3] == ["-m", "paperforge.compute._windows_appcontainer"]
    config = json.loads(
        (job_dir / "windows-appcontainer.json").read_text(encoding="utf-8")
    )
    assert config["profile_name"] == "PaperForge_" + ("b" * 48)
    assert config["command"][0] == str(executable)
    assert str(workdir / "result.txt") in config["write_roots"]


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
        env={"RUN_KIND": "evaluation"},
        outputs=["metrics.json"],
    )

    for backend in _all_backends(tmp_path):
        serialized = json.dumps(backend.submit(spec).to_dict(), sort_keys=True)
        assert "RUN_KIND" in serialized


def test_remote_execution_rejects_sensitive_environment_values(tmp_path: Path) -> None:
    del tmp_path
    with pytest.raises(ValueError, match="environment must not contain credentials"):
        JobSpec(
            name="remote-secret",
            command=["true"],
            env={"API_TOKEN": "sensitive-fixture-value"},
        )


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

    deadline = time.monotonic() + _local_test_timeout()
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


def test_local_backend_recovers_completed_state_without_secret_environment(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
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
                "print('durable output')",
            ],
        ),
        execute=True,
    )
    current = submitted
    deadline = time.monotonic() + _local_test_timeout()
    while current.status in {JobStatus.SUBMITTED, JobStatus.RUNNING}:
        assert time.monotonic() < deadline
        time.sleep(0.02)
        current = backend.status(submitted.job_id, execute=True)

    assert current.status is JobStatus.SUCCEEDED
    logs = backend.logs(submitted.job_id, execute=True)
    assert logs.stdout.strip() == "durable output"
    raw_log = next((state_dir / "local").rglob("job.log")).read_text(
        encoding="utf-8"
    )
    assert raw_log.strip() == "durable output"

    recovered = LocalBackend(
        policy=ExecutionPolicy(ExecutionProfile.FULL),
        state_dir=state_dir,
    ).status(submitted.job_id, execute=True)
    assert recovered.status is JobStatus.SUCCEEDED
    assert recovered.job_id == submitted.job_id


def test_local_backend_recovers_live_job_after_backend_restart(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    submitted = LocalBackend(
        policy=ExecutionPolicy(ExecutionProfile.FULL),
        state_dir=state_dir,
    ).submit(
        JobSpec(
            name="restart-live",
            command=[
                sys.executable,
                "-c",
                "import time; time.sleep(0.2); print('finished')",
            ],
        ),
        execute=True,
    )
    recovered_backend = LocalBackend(
        policy=ExecutionPolicy(ExecutionProfile.FULL),
        state_dir=state_dir,
    )

    current = recovered_backend.status(submitted.job_id, execute=True)
    assert current.status is JobStatus.RUNNING
    deadline = time.monotonic() + _local_test_timeout()
    while current.status is JobStatus.RUNNING:
        assert time.monotonic() < deadline
        time.sleep(0.02)
        current = recovered_backend.status(submitted.job_id, execute=True)

    assert current.status is JobStatus.SUCCEEDED
    assert current.return_code == 0
    assert (
        recovered_backend.logs(submitted.job_id, execute=True).stdout.strip()
        == "finished"
    )


def test_local_backend_does_not_persist_or_inherit_host_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = "sk-" + ("h" * 24)
    monkeypatch.setenv("OPENAI_API_KEY", canary)
    backend = LocalBackend(
        policy=ExecutionPolicy(ExecutionProfile.FULL),
        state_dir=tmp_path / "state",
    )
    submitted = backend.submit(
        JobSpec(
            name="clean-environment",
            command=[
                sys.executable,
                "-c",
                "import os; print(os.getenv('OPENAI_API_KEY', 'absent'))",
            ],
        ),
        execute=True,
    )
    deadline = time.monotonic() + _local_test_timeout()
    current = submitted
    while current.status is JobStatus.RUNNING:
        assert time.monotonic() < deadline
        time.sleep(0.02)
        current = backend.status(submitted.job_id, execute=True)

    assert current.status is JobStatus.SUCCEEDED
    launch = next((tmp_path / "state" / "local").rglob("launch.json"))
    assert canary not in launch.read_text(encoding="utf-8")
    assert backend.logs(submitted.job_id, execute=True).stdout.strip() == "absent"


def test_local_backend_sandbox_blocks_external_writes_and_redacts_raw_log(
    tmp_path: Path,
) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("before", encoding="utf-8")
    workdir = tmp_path / "work"
    workdir.mkdir()
    backend = LocalBackend(
        policy=ExecutionPolicy(ExecutionProfile.FULL),
        state_dir=tmp_path / "state",
    )
    blocked = backend.submit(
        JobSpec(
            name="sandbox-write",
            command=[
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; "
                    f"Path({str(outside)!r}).write_text('after')"
                ),
            ],
            workdir=workdir,
        ),
        execute=True,
    )
    deadline = time.monotonic() + _local_test_timeout()
    current = blocked
    while current.status in {JobStatus.SUBMITTED, JobStatus.RUNNING}:
        assert time.monotonic() < deadline
        time.sleep(0.02)
        current = backend.status(blocked.job_id, execute=True)
    assert current.status is JobStatus.FAILED
    assert outside.read_text(encoding="utf-8") == "before"

    canary = "sk-" + ("x" * 24)
    redacted = backend.submit(
        JobSpec(
            name="sandbox-redaction",
            command=[
                sys.executable,
                "-c",
                "print('sk-' + ('x' * 24))",
            ],
            workdir=workdir,
        ),
        execute=True,
    )
    deadline = time.monotonic() + _local_test_timeout()
    current = redacted
    while current.status in {JobStatus.SUBMITTED, JobStatus.RUNNING}:
        assert time.monotonic() < deadline
        time.sleep(0.02)
        current = backend.status(redacted.job_id, execute=True)
    assert current.status is JobStatus.SUCCEEDED
    raw_log = next(
        (tmp_path / "state" / "local" / redacted.job_id).glob("job.log")
    ).read_text(encoding="utf-8")
    assert canary not in raw_log
    assert "***redacted***" in raw_log


def test_local_backend_sandbox_cannot_reach_host_loopback(
    tmp_path: Path,
) -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = int(listener.getsockname()[1])
    workdir = tmp_path / "work"
    workdir.mkdir()
    backend = LocalBackend(
        policy=ExecutionPolicy(ExecutionProfile.FULL),
        state_dir=tmp_path / "state",
    )
    try:
        submitted = backend.submit(
            JobSpec(
                name="sandbox-network",
                command=[
                    sys.executable,
                    "-c",
                    (
                        "import socket; "
                        f"socket.create_connection(('127.0.0.1', {port}), 0.5)"
                    ),
                ],
                workdir=workdir,
            ),
            execute=True,
        )
        current = submitted
        deadline = time.monotonic() + _local_test_timeout()
        while current.status in {JobStatus.SUBMITTED, JobStatus.RUNNING}:
            assert time.monotonic() < deadline
            time.sleep(0.02)
            current = backend.status(submitted.job_id, execute=True)
    finally:
        listener.close()

    assert current.status is JobStatus.FAILED


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


def test_ssh_default_container_user_binds_non_root_remote_identity(
    tmp_path: Path,
) -> None:
    base = _ssh_config(tmp_path)
    config = SSHConfig(
        host=base.host,
        user=base.user,
        known_hosts_file=base.known_hosts_file,
        identity_file=base.identity_file,
        remote_container_runtime="docker",
        remote_container_runtime_sha256="b" * 64,
        remote_container_image="fixture@sha256:" + ("c" * 64),
    )
    backend = SSHBackend(config, state_dir=tmp_path / "state")
    spec = JobSpec(
        name="default-container-user",
        command=["python", "run.py"],
        workdir=tmp_path,
        outputs=["result.json"],
        metadata={"remote_source_sha256": "f" * 64},
        execute=True,
    )

    script = backend.submit(spec, execute=False).plan

    assert config.remote_container_user == "host"
    assert script is not None
    assert '"$(id -u):$(id -g)"' in script.argv[-1]
    assert '[ "$(id -u)" -ne 0 ]' in script.argv[-1]


def test_ssh_submit_transport_timeout_attempts_cleanup_and_persists_failure(
    tmp_path: Path,
) -> None:
    runner = _SSHLaunchTimeoutRunner()
    backend = SSHBackend(
        _ssh_config(tmp_path),
        runner=runner,
        policy=ExecutionPolicy(ExecutionProfile.FULL),
        state_dir=tmp_path / "state",
    )

    result = backend.submit(
        JobSpec(
            name="transport-timeout",
            job_id="transport-timeout",
            command=["true"],
            execute=True,
        ),
        execute=True,
    )

    assert result.status is JobStatus.FAILED
    assert result.metadata["submission_intent"] is False
    assert result.metadata["cleanup_attempted"] is True
    assert result.metadata["cleanup_status"] == "CLEANED"
    assert len(runner.commands) == 2
    assert SSHBackend(
        _ssh_config(tmp_path),
        runner=runner,
        policy=ExecutionPolicy(ExecutionProfile.FULL),
        state_dir=tmp_path / "state",
    )._known_result(result.job_id).status is JobStatus.FAILED

    resumed = backend.resume(result.job_id, execute=True)
    assert resumed.status is JobStatus.FAILED
    assert resumed.metadata["attempt"] == 2
    assert resumed.metadata["submission_intent"] is False
    assert resumed.metadata["cleanup_status"] == "CLEANED"
    assert len(runner.commands) == 4


def test_ssh_unconfirmed_cleanup_stays_pending_and_blocks_resume(
    tmp_path: Path,
) -> None:
    runner = _SSHUnreachableRunner()
    backend = SSHBackend(
        _ssh_config(tmp_path),
        runner=runner,
        policy=ExecutionPolicy(ExecutionProfile.FULL),
        state_dir=tmp_path / "state",
    )
    spec = JobSpec(
        name="unreachable-submit",
        job_id="unreachable-submit",
        command=["true"],
        execute=True,
    )

    result = backend.submit(spec, execute=True)

    assert result.status is JobStatus.UNKNOWN
    assert result.metadata["submission_intent"] is True
    assert result.metadata["cleanup_pending"] is True
    assert result.metadata["cleanup_status"] == "FAILED"
    persisted = SSHBackend(
        _ssh_config(tmp_path),
        runner=runner,
        policy=ExecutionPolicy(ExecutionProfile.FULL),
        state_dir=tmp_path / "state",
    )
    pending = persisted.status(result.job_id, execute=True)
    assert pending.status is JobStatus.UNKNOWN
    assert pending.metadata["cleanup_pending"] is True
    with pytest.raises(RuntimeError, match="unresolved SSH launch"):
        persisted.resume(result.job_id, execute=True)
    assert len(runner.commands) == 4


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


@pytest.mark.skipif(os.name == "nt", reason="symlink fixture requires POSIX semantics")
@pytest.mark.parametrize("direction", [ArtifactDirection.UPLOAD, ArtifactDirection.DOWNLOAD])
def test_ssh_artifact_sync_rejects_nested_local_symlink(
    tmp_path: Path,
    direction: ArtifactDirection,
) -> None:
    backend = SSHBackend(
        _ssh_config(tmp_path),
        state_dir=tmp_path / "state",
    )
    submitted = backend.submit(
        JobSpec(
            name="nested-symlink",
            command=["true"],
            outputs=["safe/result.json"],
        )
    )
    local_root = tmp_path / "artifacts"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "result.json").write_text("secret", encoding="utf-8")
    local_root.mkdir()
    (local_root / "safe").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symbolic link"):
        backend.sync_artifacts(
            submitted.job_id,
            local_root,
            direction=direction,
        )


@pytest.mark.skipif(os.name == "nt", reason="symlink fixture requires POSIX semantics")
def test_kubernetes_download_rejects_nested_destination_symlink(
    tmp_path: Path,
) -> None:
    image = "fixture@sha256:" + ("a" * 64)
    backend = KubernetesBackend(
        KubernetesConfig(
            image=image,
            source_pvc="source-pvc",
            artifact_pvc="artifact-pvc",
            source_transport_image=image,
        ),
        state_dir=tmp_path / "state",
    )
    spec = JobSpec(
        name="nested-kubernetes-symlink",
        job_id="nested-kubernetes-symlink",
        command=["python", "run.py"],
        workdir=tmp_path,
        outputs=["safe/result.json"],
        metadata={"source_snapshot_sha256": "b" * 64},
        execute=True,
    )
    backend.submit(spec, execute=False)
    local_root = tmp_path / "download"
    outside = tmp_path / "outside"
    outside.mkdir()
    local_root.mkdir()
    (local_root / "safe").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symbolic link"):
        backend.sync_artifacts(spec.job_id or "", local_root)


@pytest.mark.skipif(os.name == "nt", reason="symlink fixture requires POSIX semantics")
def test_legacy_kubernetes_download_uses_private_staging_and_rejects_symlink(
    tmp_path: Path,
) -> None:
    runner = _KubernetesArtifactRunner()
    backend = KubernetesBackend(
        KubernetesConfig(image="python:3.12-slim"),
        runner=runner,
        policy=ExecutionPolicy(ExecutionProfile.FULL),
        state_dir=tmp_path / "state",
    )
    spec = JobSpec(
        name="legacy-kubernetes",
        job_id="legacy-kubernetes",
        command=["true"],
        outputs=["safe/result.json"],
    )
    backend.submit(spec, execute=False)
    local_root = tmp_path / "download"
    outside = tmp_path / "outside"
    outside.mkdir()
    local_root.mkdir()
    (local_root / "safe").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symbolic link"):
        backend.sync_artifacts(spec.job_id or "", local_root, execute=True)
    assert not (outside / "result.json").exists()
    assert runner.commands == []

    (local_root / "safe").unlink()
    synced = backend.sync_artifacts(spec.job_id or "", local_root, execute=True)
    assert synced.status is JobStatus.SUCCEEDED
    assert (local_root / "safe" / "result.json").read_text() == "REMOTE_DATA"
    copy_command = next(command for command in runner.commands if "cp" in command)
    assert str(tmp_path / "state") in copy_command[-1]
    assert str(local_root) not in copy_command[-1]


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS system alias contract")
def test_artifact_root_accepts_trusted_macos_var_alias() -> None:
    from paperforge.compute._artifacts import safe_artifact_root

    with tempfile.TemporaryDirectory(dir="/var/tmp") as temporary:
        root = safe_artifact_root(temporary, create=False)

    assert str(root).startswith("/private/var/")


def test_local_resume_uses_fresh_attempt_artifacts(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    state_dir = tmp_path / "state"
    backend = LocalBackend(
        policy=ExecutionPolicy(ExecutionProfile.FULL),
        state_dir=state_dir,
    )
    code = (
        "from pathlib import Path; p=Path('result.txt'); "
        "p.write_text('stale' if p.exists() and p.read_text() else 'fresh')"
    )
    submitted = backend.submit(
        JobSpec(
            name="fresh-attempt",
            job_id="fresh-attempt",
            command=[sys.executable, "-c", code],
            workdir=workdir,
            outputs=["result.txt"],
            resources=ResourceSpec(timeout_seconds=_local_process_timeout()),
            execute=True,
        )
    )

    current = submitted
    deadline = time.monotonic() + _local_test_timeout()
    while current.status is JobStatus.RUNNING:
        assert time.monotonic() < deadline
        time.sleep(0.02)
        current = backend.status(submitted.job_id, execute=True)
    assert current.status is JobStatus.SUCCEEDED
    first = backend.sync_artifacts(
        submitted.job_id,
        tmp_path / "first",
        execute=True,
    )
    assert first.artifacts[0].attempt_id == 1
    assert (tmp_path / "first" / "result.txt").read_text() == "fresh"

    resumed = backend.resume(submitted.job_id, execute=True)
    current = resumed
    deadline = time.monotonic() + _local_test_timeout()
    while current.status is JobStatus.RUNNING:
        assert time.monotonic() < deadline
        time.sleep(0.02)
        current = backend.status(submitted.job_id, execute=True)
    assert current.status is JobStatus.SUCCEEDED
    second = backend.sync_artifacts(
        submitted.job_id,
        tmp_path / "second",
        execute=True,
    )
    assert second.artifacts[0].attempt_id == 2
    assert (tmp_path / "second" / "result.txt").read_text() == "fresh"
    assert not (workdir / "result.txt").exists()


def test_docker_deadline_forces_exact_container_and_records_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    runner = _DockerDeadlineRunner()
    clock = iter((100.0, 102.0))
    monkeypatch.setattr(
        "paperforge.compute.docker.time.time",
        lambda: next(clock),
    )
    backend = DockerBackend(
        DockerConfig(
            image="fixture@sha256:" + ("a" * 64),
            workspace_mount=source,
            container_user="65532:65532",
        ),
        runner=runner,
        policy=ExecutionPolicy(ExecutionProfile.FULL),
        state_dir=tmp_path / "state",
    )
    submitted = backend.submit(
        JobSpec(
            name="deadline",
            job_id="deadline",
            command=["python", "run.py"],
            workdir=source,
            outputs=["result.json"],
            resources=ResourceSpec(timeout_seconds=1),
            execute=True,
        ),
        execute=True,
    )

    result = backend.status(submitted.job_id, execute=True)

    assert result.status is JobStatus.FAILED
    assert result.return_code == 124
    assert result.metadata["timed_out"] is True
    assert any(command[1:3] == ("rm", "--force") for command in runner.commands)

    repeated = backend.status(submitted.job_id, execute=True)
    assert repeated.status is JobStatus.FAILED
    assert repeated.return_code == 124
    assert repeated.metadata["timed_out"] is True


def test_docker_deadline_cleanup_failure_blocks_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CleanupFailureRunner(_DockerDeadlineRunner):
        def run(self, argv, **kwargs) -> CommandOutcome:
            command = tuple(argv)
            if command[1:3] == ("rm", "--force"):
                self.commands.append(command)
                return CommandOutcome(1, "", "container removal denied\n")
            return super().run(argv, **kwargs)

    source = tmp_path / "source"
    source.mkdir()
    runner = CleanupFailureRunner()
    clock = iter((100.0, 102.0, 103.0))
    monkeypatch.setattr(
        "paperforge.compute.docker.time.time",
        lambda: next(clock),
    )
    backend = DockerBackend(
        DockerConfig(
            image="fixture@sha256:" + ("a" * 64),
            workspace_mount=source,
            container_user="65532:65532",
        ),
        runner=runner,
        policy=ExecutionPolicy(ExecutionProfile.FULL),
        state_dir=tmp_path / "state",
    )
    submitted = backend.submit(
        JobSpec(
            name="cleanup-failure",
            job_id="cleanup-failure",
            command=["python", "run.py"],
            workdir=source,
            outputs=["result.json"],
            resources=ResourceSpec(timeout_seconds=1),
            execute=True,
        ),
        execute=True,
    )

    result = backend.status(submitted.job_id, execute=True)

    assert result.status is JobStatus.RUNNING
    assert result.return_code is None
    assert result.metadata["cleanup_pending"] is True
    assert "timed_out" not in result.metadata
    with pytest.raises(RuntimeError, match="cleanup is pending"):
        backend.resume(submitted.job_id, execute=True)


@pytest.mark.parametrize(
    ("exit_status", "exit_code"),
    (("exited", "0"), ("exited", "1"), ("dead", "137")),
)
def test_docker_cleanup_pending_cannot_become_terminal_without_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exit_status: str,
    exit_code: str,
) -> None:
    class CleanupThenExitRunner(_DockerDeadlineRunner):
        def __init__(self) -> None:
            super().__init__()
            self.exited = False

        def run(self, argv, **kwargs) -> CommandOutcome:
            command = tuple(argv)
            if command[1:3] == ("rm", "--force"):
                self.commands.append(command)
                if not self.exited:
                    self.exited = True
                    return CommandOutcome(1, "", "container removal denied\n")
                self.removed = True
                return CommandOutcome(0, command[-1] + "\n", "")
            if command[1:3] == ("inspect", "--format") and self.exited:
                self.commands.append(command)
                if self.removed:
                    return CommandOutcome(1, "", "No such container\n")
                if command[3] == "{{.Id}}":
                    return CommandOutcome(0, self.container_id + "\n", "")
                return CommandOutcome(
                    0,
                    f"{self.container_id}|{exit_status}|{exit_code}\n",
                    "",
                )
            return super().run(argv, **kwargs)

    source = tmp_path / "source"
    source.mkdir()
    runner = CleanupThenExitRunner()
    clock = iter((100.0, 102.0, 103.0))
    monkeypatch.setattr(
        "paperforge.compute.docker.time.time",
        lambda: next(clock),
    )
    backend = DockerBackend(
        DockerConfig(
            image="fixture@sha256:" + ("a" * 64),
            workspace_mount=source,
            container_user="65532:65532",
        ),
        runner=runner,
        policy=ExecutionPolicy(ExecutionProfile.FULL),
        state_dir=tmp_path / "state",
    )
    submitted = backend.submit(
        JobSpec(
            name="cleanup-then-exit",
            job_id="cleanup-then-exit",
            command=["python", "run.py"],
            workdir=source,
            resources=ResourceSpec(timeout_seconds=1),
            execute=True,
        ),
        execute=True,
    )

    pending = backend.status(submitted.job_id, execute=True)
    terminal = backend.status(submitted.job_id, execute=True)

    assert pending.metadata["cleanup_pending"] is True
    assert terminal.status is JobStatus.FAILED
    assert terminal.return_code == 124
    assert terminal.metadata["timed_out"] is True


def test_ssh_status_and_cancel_bind_pid_start_container_and_binding(
    tmp_path: Path,
) -> None:
    base = _ssh_config(tmp_path)
    config = SSHConfig(
        host=base.host,
        user=base.user,
        known_hosts_file=base.known_hosts_file,
        identity_file=base.identity_file,
        remote_container_runtime="docker",
        remote_container_runtime_sha256="b" * 64,
        remote_container_image="fixture@sha256:" + ("c" * 64),
    )
    runner = _SSHIdentityRunner()
    backend = SSHBackend(
        config,
        runner=runner,
        policy=ExecutionPolicy(ExecutionProfile.FULL),
        state_dir=tmp_path / "state",
    )
    submitted = backend.submit(
        JobSpec(
            name="identity",
            job_id="identity",
            command=["python", "run.py"],
            workdir=tmp_path,
            outputs=["result.json"],
            metadata={"remote_source_sha256": "f" * 64},
            execute=True,
        ),
        execute=True,
    )
    status_script = backend.status(submitted.job_id).plan
    cancel_script = backend.cancel(submitted.job_id).plan
    assert status_script is not None and cancel_script is not None
    combined = status_script.argv[-1] + cancel_script.argv[-1]
    assert "/proc/123/stat" in combined
    assert _SSHIdentityRunner.container_id in combined
    assert str(submitted.metadata["binding_digest"]) in combined

    tampered = submitted.to_dict()
    tampered["metadata"]["remote_start_time"] = "999"
    backend._results[submitted.job_id] = type(submitted).from_dict(tampered)
    before = len(runner.commands)
    refused = backend.cancel(submitted.job_id, execute=True)
    assert refused.status is JobStatus.UNKNOWN
    assert len(runner.commands) == before + 1
    assert "if [" in runner.commands[-1][-1]
    assert "then" in runner.commands[-1][-1]


@pytest.mark.skipif(os.name == "nt", reason="generated SSH script requires POSIX sh")
@pytest.mark.parametrize(
    ("runtime", "name_prefix"),
    (("docker", "/"), ("podman", "")),
)
def test_ssh_timeout_status_waits_for_bound_container_cleanup(
    tmp_path: Path,
    runtime: str,
    name_prefix: str,
) -> None:
    base = _ssh_config(tmp_path)
    runtime_state = tmp_path / "runtime-state"
    runtime_state.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_runtime = fake_bin / runtime
    fake_runtime.write_text(
        """#!/bin/sh
state=${PAPERFORGE_FAKE_RUNTIME_STATE:?}
cid=${PAPERFORGE_FAKE_CONTAINER_ID:?}
name=${PAPERFORGE_FAKE_CONTAINER_NAME:?}
prefix=${PAPERFORGE_FAKE_NAME_PREFIX-}
case "$1" in
  inspect)
    [ ! -f "$state/daemon_down" ] || exit 1
    [ ! -f "$state/inspect_fail" ] || exit 1
    [ -f "$state/container_exists" ] || exit 1
    if [ "$3" = "{{.Id}}|{{.Name}}" ]; then
      printf '%s|%s%s\\n' "$cid" "$prefix" "$name"
    else
      printf '%s\\n' "$cid"
    fi
    ;;
  rm)
    [ ! -f "$state/rm_fail" ] || exit 1
    rm -f "$state/container_exists"
    ;;
  ps)
    [ ! -f "$state/daemon_down" ] || exit 1
    if [ -f "$state/container_exists" ]; then
      printf '%s\\n' "$cid"
    fi
    ;;
  info)
    [ ! -f "$state/daemon_down" ] || exit 1
    ;;
  *)
    exit 2
    ;;
esac
""",
        encoding="utf-8",
    )
    fake_runtime.chmod(0o755)
    config = SSHConfig(
        host=base.host,
        user=base.user,
        known_hosts_file=base.known_hosts_file,
        identity_file=base.identity_file,
        remote_root="remote/jobs",
        remote_container_runtime=runtime,
        remote_container_runtime_sha256="b" * 64,
        remote_container_image="fixture@sha256:" + ("c" * 64),
    )
    container_name = SSHBackend._remote_container_name("timeout-cleanup", 1)
    runner = _SSHTimeoutCleanupRunner(
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "PAPERFORGE_FAKE_RUNTIME_STATE": str(runtime_state),
            "PAPERFORGE_FAKE_CONTAINER_ID": _SSHTimeoutCleanupRunner.container_id,
            "PAPERFORGE_FAKE_CONTAINER_NAME": container_name,
            "PAPERFORGE_FAKE_NAME_PREFIX": name_prefix,
        },
    )
    backend = SSHBackend(
        config,
        runner=runner,
        policy=ExecutionPolicy(ExecutionProfile.FULL),
        state_dir=tmp_path / "state",
    )
    submitted = backend.submit(
        JobSpec(
            name="timeout-cleanup",
            job_id="timeout-cleanup",
            command=["python", "run.py"],
            workdir=tmp_path,
            metadata={"remote_source_sha256": "f" * 64},
            resources=ResourceSpec(timeout_seconds=1),
            execute=True,
        ),
        execute=True,
    )
    attempt_dir = tmp_path / "remote" / "jobs" / submitted.job_id / "attempts" / "1"
    attempt_dir.mkdir(parents=True)
    (attempt_dir / "pid").write_text("123\n", encoding="utf-8")
    (attempt_dir / "pid_start").write_text("456\n", encoding="utf-8")
    (attempt_dir / "identity").write_text(
        "\n".join(
            (
                str(submitted.metadata["identity_nonce"]),
                str(submitted.metadata["binding_digest"]),
                "1",
                "",
            )
        ),
        encoding="utf-8",
    )
    (attempt_dir / "container_id").write_text(
        _SSHTimeoutCleanupRunner.container_id + "\n",
        encoding="utf-8",
    )
    (attempt_dir / "timed_out").write_text("TIMED_OUT\n", encoding="utf-8")
    (runtime_state / "container_exists").touch()

    (runtime_state / "inspect_fail").touch()
    inspect_unavailable = backend.status(submitted.job_id, execute=True)
    (runtime_state / "inspect_fail").unlink()
    (attempt_dir / "timeout_cleanup_pending").write_text(
        "e" * 64 + "\n",
        encoding="utf-8",
    )
    marker_mismatch = backend.status(submitted.job_id, execute=True)
    with pytest.raises(RuntimeError, match="unresolved SSH cleanup"):
        backend.resume(submitted.job_id, execute=True)
    (attempt_dir / "timeout_cleanup_pending").write_text(
        _SSHTimeoutCleanupRunner.container_id + "\n",
        encoding="utf-8",
    )
    (runtime_state / "daemon_down").touch()
    daemon_unavailable = backend.status(submitted.job_id, execute=True)
    (runtime_state / "daemon_down").unlink()
    (runtime_state / "rm_fail").touch()
    cleanup_retry = backend.status(submitted.job_id, execute=True)
    (runtime_state / "rm_fail").unlink()
    terminal = backend.status(submitted.job_id, execute=True)
    (runtime_state / "daemon_down").touch()
    repeated = backend.status(submitted.job_id, execute=True)
    submit_script = runner.commands[0][-1]
    status_script = runner.commands[1][-1]
    timeout_branch = status_script.split("elif [ -f", 1)[1].split("elif [ -f", 1)[0]

    assert "timeout_cleanup_pending" in submit_script
    assert "timeout_cleanup_done" in submit_script
    assert "'{{.Id}}|{{.Name}}'" in submit_script
    assert 'rm -f "$cid"' in submit_script
    assert "TIMED_OUT|124" in status_script
    assert "inspect --format" in timeout_branch
    assert "rm -f" in timeout_branch
    assert "ps -a --no-trunc" in timeout_branch
    assert " info " in timeout_branch
    assert "CLEANUP_PENDING" in timeout_branch
    assert inspect_unavailable.status is JobStatus.RUNNING
    assert inspect_unavailable.metadata["timeout_cleanup_pending"] is True
    assert marker_mismatch.status is JobStatus.UNKNOWN
    assert marker_mismatch.metadata["timeout_cleanup_pending"] is True
    assert daemon_unavailable.status is JobStatus.RUNNING
    assert daemon_unavailable.metadata["timeout_cleanup_pending"] is True
    assert "timed_out" not in daemon_unavailable.metadata
    assert cleanup_retry.status is JobStatus.RUNNING
    assert cleanup_retry.metadata["timeout_cleanup_pending"] is True
    assert terminal.status is JobStatus.FAILED
    assert terminal.return_code == 124
    assert "timeout_cleanup_pending" not in terminal.metadata
    assert terminal.metadata["timed_out"] is True
    assert repeated.status is JobStatus.FAILED
    assert repeated.return_code == 124
    assert (attempt_dir / "timeout_cleanup_done").read_text().strip() == (
        _SSHTimeoutCleanupRunner.container_id
    )
    assert not (attempt_dir / "timeout_cleanup_pending").exists()
    assert not (runtime_state / "container_exists").exists()


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


def test_executable_kubernetes_manifest_is_non_root_networkless_and_read_only(
    tmp_path: Path,
) -> None:
    image = "fixture@sha256:" + ("a" * 64)
    backend = KubernetesBackend(
        KubernetesConfig(
            image=image,
            source_pvc="source-pvc",
            artifact_pvc="artifact-pvc",
            source_transport_image=image,
        ),
        state_dir=tmp_path / "state",
    )
    spec = JobSpec(
        name="secure-kubernetes",
        job_id="secure-kubernetes",
        command=["python", "run.py"],
        workdir=tmp_path,
        outputs=["nested/result.json"],
        metadata={"source_snapshot_sha256": "b" * 64},
        execute=True,
    )

    manifest = backend._manifest(spec, spec.job_id or "")
    policy, job = manifest["items"]
    assert policy["kind"] == "NetworkPolicy"
    assert policy["spec"]["policyTypes"] == ["Ingress", "Egress"]
    assert policy["spec"]["ingress"] == []
    assert policy["spec"]["egress"] == []
    pod = job["spec"]["template"]
    assert policy["spec"]["podSelector"]["matchLabels"] == pod["metadata"][
        "labels"
    ]
    pod_spec = pod["spec"]
    assert pod_spec["automountServiceAccountToken"] is False
    assert pod_spec["securityContext"]["runAsUser"] == 65532
    assert pod_spec["securityContext"]["runAsGroup"] == 65532
    container = pod_spec["containers"][0]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    mounts = {mount["mountPath"]: mount for mount in container["volumeMounts"]}
    assert mounts["/workspace"]["readOnly"] is True
    assert mounts["/paperforge-outputs/0"]["subPath"].endswith(
        "/attempts/1/nested/result.json"
    )
    init_by_name = {
        container["name"]: container for container in pod_spec["initContainers"]
    }
    assert "rm -rf" in init_by_name["artifact-prepare"]["command"][2]
    assert "network-policy-settle" not in init_by_name


def test_executable_kubernetes_artifacts_use_live_transport_pod(
    tmp_path: Path,
) -> None:
    image = "fixture@sha256:" + ("a" * 64)
    backend = KubernetesBackend(
        KubernetesConfig(
            image=image,
            source_pvc="source-pvc",
            artifact_pvc="artifact-pvc",
            source_transport_image=image,
        ),
        state_dir=tmp_path / "state",
    )
    spec = JobSpec(
        name="transport-kubernetes",
        job_id="transport-kubernetes",
        command=["python", "run.py"],
        workdir=tmp_path,
        outputs=["result.json"],
        metadata={"source_snapshot_sha256": "b" * 64},
        execute=True,
    )
    backend.submit(spec, execute=False)

    plan = backend.sync_artifacts(
        spec.job_id or "",
        tmp_path / "download",
        execute=False,
    )
    serialized = json.dumps(plan.to_dict(), sort_keys=True)

    assert "artifact-transport" not in serialized
    assert "transport_pod" in serialized
    assert "jsonpath={.items[0].metadata.name}" not in serialized
    assert (
        "/pvc-artifacts/artifacts/transport-kubernetes/attempts/1/result.json"
        in serialized
    )


def test_kubernetes_failure_target_is_terminal_failure() -> None:
    assert KubernetesBackend._status_from_job(
        {
            "status": {
                "conditions": [
                    {"type": "FailureTarget", "status": "True"},
                ]
            }
        }
    ) is JobStatus.FAILED


def test_compute_state_rejects_preexisting_job_symlink(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    (state / "local").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (state / "local" / "fixed-job").symlink_to(
        outside,
        target_is_directory=True,
    )
    workdir = tmp_path / "work"
    workdir.mkdir()
    backend = LocalBackend(
        policy=ExecutionPolicy(ExecutionProfile.FULL),
        state_dir=state,
    )

    with pytest.raises(UnsafePathError, match="symbolic link"):
        backend.submit(
            JobSpec(
                name="state-symlink",
                job_id="fixed-job",
                command=[sys.executable, "-c", "print('blocked')"],
                workdir=workdir,
            ),
            execute=True,
        )

    assert list(outside.iterdir()) == []


def test_source_bundle_scans_credentials_beyond_first_two_megabytes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    payload = b"x" * (2 * 1024 * 1024 + 128)
    payload += b"\nsk-" + b"z" * 24 + b"\n"
    file_path = source / "notes.txt"
    file_path.write_bytes(payload)
    record = {
        "path": str(file_path),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
    }
    expected = hashlib.sha256(
        json.dumps(
            {"files": [record]},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()

    with pytest.raises(SourceBundleError, match="credential-like"):
        create_verified_source_bundle(
            source,
            canonical_worktree=source,
            expected_source_sha256=expected,
            staging_dir=tmp_path / "stage",
        )
