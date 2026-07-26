from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from paperforge.compute import (
    ArtifactDirection,
    CloudSSHBackend,
    CloudSSHConfig,
    DockerBackend,
    DockerConfig,
    JobResult,
    JobSpec,
    JobStatus,
    KubernetesBackend,
    KubernetesConfig,
    ResourceSpec,
    SlurmBackend,
    SlurmConfig,
    SSHBackend,
    SSHConfig,
    build_compute_binding,
)
from paperforge.models import ExecutionProfile
from paperforge.policy import ExecutionPolicy

_DOCKER_IMAGE = (
    "python@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de"
)
_OPENSSH_FIXTURE_IMAGE = "paperforge-openssh-fixture:v3"


def _wait_for_terminal(backend: DockerBackend, job_id: str) -> JobStatus:
    deadline = time.monotonic() + 30
    current = backend.status(job_id, execute=True)
    while current.status in {
        JobStatus.SUBMITTED,
        JobStatus.QUEUED,
        JobStatus.RUNNING,
    }:
        assert time.monotonic() < deadline
        time.sleep(0.1)
        current = backend.status(job_id, execute=True)
    return current.status


def _shared_runtime_root(tmp_path: Path, name: str) -> Path:
    if sys.platform == "darwin":
        root = (
            Path.home()
            / "Library"
            / "Caches"
            / "PaperForge"
            / name
            / uuid.uuid4().hex
        )
    else:
        root = tmp_path / name
    root.mkdir(parents=True)
    return root


@pytest.mark.skipif(
    os.environ.get("PAPERFORGE_REAL_DOCKER") != "1",
    reason="real Docker integration gate is not enabled",
)
def test_real_docker_lifecycle_and_isolation(tmp_path: Path) -> None:
    if shutil.which("docker") is None:
        pytest.fail("Docker integration gate requires docker")
    inspected = subprocess.run(
        ["docker", "image", "inspect", _DOCKER_IMAGE],
        check=False,
        capture_output=True,
        text=True,
    )
    assert inspected.returncode == 0, inspected.stderr

    runtime_root = _shared_runtime_root(tmp_path, "real-docker")
    worktree = runtime_root / "worktree"
    worktree.mkdir()
    backend = DockerBackend(
        DockerConfig(image=_DOCKER_IMAGE, workspace_mount=worktree),
        policy=ExecutionPolicy(ExecutionProfile.FULL),
        state_dir=runtime_root / "state",
    )
    containers: list[str] = []
    listener: socket.socket | None = None
    try:
        submitted = backend.submit(
            JobSpec(
                name="docker-integration",
                job_id="docker-integration",
                command=[
                    "python",
                    "-c",
                    (
                        "from pathlib import Path; "
                        "Path('result.json').write_text('{\"ok\": true}'); "
                        "print('docker-integration-ok')"
                    ),
                ],
                workdir=worktree,
                outputs=["result.json"],
                execute=True,
            )
        )
        containers.append(str(submitted.metadata["container_name"]))
        assert _wait_for_terminal(
            backend,
            submitted.job_id,
        ) is JobStatus.SUCCEEDED, backend.logs(
            submitted.job_id,
            execute=True,
        ).stderr
        assert "docker-integration-ok" in backend.logs(
            submitted.job_id,
            execute=True,
        ).stdout
        synced = backend.sync_artifacts(
            submitted.job_id,
            runtime_root / "synced",
            direction=ArtifactDirection.DOWNLOAD,
            execute=True,
        )
        assert synced.status is JobStatus.SUCCEEDED
        assert json.loads(
            (runtime_root / "synced" / "result.json").read_text(encoding="utf-8")
        ) == {"ok": True}

        retry = backend.submit(
            JobSpec(
                name="docker-retry",
                job_id="docker-retry",
                command=[
                    "python",
                    "-c",
                    (
                        "from pathlib import Path; p=Path('retry.json'); "
                        "assert not p.read_text(); p.write_text('fresh-attempt')"
                    ),
                ],
                workdir=worktree,
                outputs=["retry.json"],
                execute=True,
            )
        )
        containers.append(str(retry.metadata["container_name"]))
        assert _wait_for_terminal(backend, retry.job_id) is JobStatus.SUCCEEDED
        retry_resumed = backend.resume(retry.job_id, execute=True)
        containers.append(str(retry_resumed.metadata["container_name"]))
        assert retry_resumed.status is JobStatus.SUBMITTED
        assert _wait_for_terminal(backend, retry.job_id) is JobStatus.SUCCEEDED
        retry_synced = backend.sync_artifacts(
            retry.job_id,
            runtime_root / "retry-synced",
            direction=ArtifactDirection.DOWNLOAD,
            execute=True,
        )
        assert retry_synced.status is JobStatus.SUCCEEDED
        assert retry_synced.artifacts[0].attempt_id == 2
        assert (runtime_root / "retry-synced" / "retry.json").read_text(
            encoding="utf-8"
        ) == "fresh-attempt"

        immutable = worktree / "immutable.txt"
        immutable.write_text("before", encoding="utf-8")
        mutation = backend.submit(
            JobSpec(
                name="docker-mutation",
                job_id="docker-mutation",
                command=[
                    "python",
                    "-c",
                    "from pathlib import Path; Path('immutable.txt').write_text('after')",
                ],
                workdir=worktree,
                execute=True,
            )
        )
        containers.append(str(mutation.metadata["container_name"]))
        assert _wait_for_terminal(backend, mutation.job_id) is JobStatus.FAILED
        assert immutable.read_text(encoding="utf-8") == "before"

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = int(listener.getsockname()[1])
        network = backend.submit(
            JobSpec(
                name="docker-network",
                job_id="docker-network",
                command=[
                    "python",
                    "-c",
                    (
                        "import socket; "
                        f"socket.create_connection(('127.0.0.1', {port}), 0.5)"
                    ),
                ],
                workdir=worktree,
                execute=True,
            )
        )
        containers.append(str(network.metadata["container_name"]))
        assert _wait_for_terminal(backend, network.job_id) is JobStatus.FAILED

        timeout_job = backend.submit(
            JobSpec(
                name="docker-timeout",
                job_id="docker-timeout",
                command=["python", "-c", "import time; time.sleep(30)"],
                workdir=worktree,
                resources=ResourceSpec(timeout_seconds=1),
                execute=True,
            )
        )
        containers.append(str(timeout_job.metadata["container_name"]))
        assert _wait_for_terminal(backend, timeout_job.job_id) is JobStatus.FAILED
        timeout_status = backend.status(timeout_job.job_id, execute=True)
        assert timeout_status.return_code == 124
        assert timeout_status.metadata["timed_out"] is True
        timeout_inspect = subprocess.run(
            ["docker", "inspect", str(timeout_job.metadata["container_name"])],
            check=False,
            capture_output=True,
            text=True,
        )
        assert timeout_inspect.returncode != 0

        long_job = backend.submit(
            JobSpec(
                name="docker-cancel",
                job_id="docker-cancel",
                command=["python", "-c", "import time; time.sleep(30)"],
                workdir=worktree,
                execute=True,
            )
        )
        containers.append(str(long_job.metadata["container_name"]))
        assert backend.cancel(long_job.job_id, execute=True).status is JobStatus.CANCELLED
    finally:
        if listener is not None:
            listener.close()
        for container in containers:
            subprocess.run(
                ["docker", "rm", "--force", container],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        shutil.rmtree(runtime_root, ignore_errors=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@pytest.mark.skipif(
    os.environ.get("PAPERFORGE_REAL_SSH") != "1",
    reason="real OpenSSH integration gate is not enabled",
)
@pytest.mark.parametrize("backend_kind", ["ssh", "cloud-ssh"])
def test_real_ssh_container_lifecycle_and_isolation(
    tmp_path: Path,
    backend_kind: str,
) -> None:
    for executable in ("docker", "ssh", "scp", "ssh-keygen", "ssh-keyscan"):
        assert shutil.which(executable), f"missing integration tool: {executable}"
    fixture_image = subprocess.run(
        ["docker", "image", "inspect", _OPENSSH_FIXTURE_IMAGE],
        check=False,
        capture_output=True,
        text=True,
    )
    assert fixture_image.returncode == 0, fixture_image.stderr
    compute_image = subprocess.run(
        ["docker", "image", "inspect", _DOCKER_IMAGE],
        check=False,
        capture_output=True,
        text=True,
    )
    assert compute_image.returncode == 0, compute_image.stderr

    fixture_id = uuid.uuid4().hex
    fixture_root = _shared_runtime_root(tmp_path, "real-ssh")
    remote_home = fixture_root / "home"
    ssh_dir = remote_home / ".ssh"
    ssh_dir.mkdir(parents=True, mode=0o700)
    identity = tmp_path / "id_ed25519"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(identity)],
        check=True,
    )
    if os.name != "nt":
        identity.chmod(0o600)
    authorized_keys = ssh_dir / "authorized_keys"
    authorized_keys.write_bytes(identity.with_suffix(".pub").read_bytes())
    if os.name != "nt":
        authorized_keys.chmod(0o600)

    fixture_name = f"paperforge-openssh-{fixture_id[:20]}"
    fixture = subprocess.run(
        [
            "docker",
            "run",
            "--detach",
            "--name",
            fixture_name,
            "--publish",
            "127.0.0.1::22",
            "--mount",
            f"type=bind,src={remote_home},dst={remote_home}",
            "--mount",
            "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock",
            "--env",
            f"PAPERFORGE_REMOTE_HOME={remote_home}",
            "--env",
            f"PAPERFORGE_REMOTE_UID={os.getuid()}",
            "--env",
            f"PAPERFORGE_REMOTE_GID={os.getgid()}",
            _OPENSSH_FIXTURE_IMAGE,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert fixture.returncode == 0, fixture.stderr

    port_result = subprocess.run(
        ["docker", "port", fixture_name, "22/tcp"],
        check=True,
        capture_output=True,
        text=True,
    )
    port = int(port_result.stdout.strip().rsplit(":", 1)[1])
    known_hosts = tmp_path / "known_hosts"
    scan: subprocess.CompletedProcess[str] | None = None
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        scan = subprocess.run(
            ["ssh-keyscan", "-T", "2", "-p", str(port), "127.0.0.1"],
            check=False,
            capture_output=True,
            text=True,
        )
        if scan.returncode == 0 and scan.stdout:
            break
        time.sleep(0.2)
    assert scan is not None and scan.returncode == 0 and scan.stdout, (
        subprocess.run(
            ["docker", "logs", fixture_name],
            check=False,
            capture_output=True,
            text=True,
        ).stderr
    )
    known_hosts.write_text(scan.stdout, encoding="utf-8")
    if os.name != "nt":
        known_hosts.chmod(0o600)
    runtime = "/usr/bin/docker"
    runtime_digest = subprocess.run(
        ["docker", "exec", fixture_name, "sha256sum", runtime],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()[0]
    assert len(runtime_digest) == 64
    remote_root = f".paperforge-integration/{fixture_id}"
    config_payload = {
        "host": "127.0.0.1",
        "user": "paperforge",
        "port": port,
        "known_hosts_file": str(known_hosts),
        "identity_file": str(identity),
        "remote_root": remote_root,
        "remote_container_runtime": runtime,
        "remote_container_runtime_sha256": runtime_digest,
        "remote_container_image": _DOCKER_IMAGE,
    }
    binding_config = (
        config_payload
        if backend_kind == "ssh"
        else {
            "provider": "integration-fixture",
            "instance_id": "localhost",
            "region": "local",
            "ssh": config_payload,
        }
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "runner.py").write_text(
        "from pathlib import Path\n"
        "Path('result.json').write_text('{\"ok\": true}', encoding='utf-8')\n"
        "print('ssh-integration-ok')\n",
        encoding="utf-8",
    )
    containers: list[str] = []
    listener: socket.socket | None = None
    try:
        def make_ssh_config() -> SSHConfig:
            return SSHConfig(
                host="127.0.0.1",
                user="paperforge",
                port=port,
                known_hosts_file=known_hosts,
                identity_file=identity,
                remote_root=remote_root,
                remote_container_runtime=runtime,
                remote_container_runtime_sha256=runtime_digest,
                remote_container_image=_DOCKER_IMAGE,
            )

        def make_backend() -> SSHBackend:
            common = {
                "policy": ExecutionPolicy(ExecutionProfile.FULL),
                "state_dir": tmp_path / "state",
            }
            if backend_kind == "ssh":
                return SSHBackend(make_ssh_config(), **common)
            return CloudSSHBackend(
                CloudSSHConfig(
                    ssh=make_ssh_config(),
                    provider="integration-fixture",
                    instance_id="localhost",
                    region="local",
                ),
                **common,
            )

        backend = make_backend()

        def stage_and_submit(
            *,
            name: str,
            command: list[str],
            outputs: list[str] | None = None,
            resources: dict[str, int] | None = None,
        ) -> tuple[SSHBackend, JobSpec]:
            spec, binding = build_compute_binding(
                workspace,
                job_spec={
                    "name": name,
                    "job_id": name,
                    "command": command,
                    "workdir": ".",
                    "outputs": outputs or [],
                    "resources": resources or {},
                    "execute": True,
                },
                compute_backend=backend_kind,
                compute_config=binding_config,
            )
            execution_worktree = Path(str(binding["execution_worktree"]))
            assert backend.stage_source(
                spec,
                execution_worktree,
                execute=True,
            ).status is JobStatus.SUCCEEDED
            submitted = backend.submit(spec)
            assert submitted.status is JobStatus.SUBMITTED
            containers.append(
                backend._remote_container_name(
                    spec.job_id or "",
                    int(submitted.metadata["attempt"]),
                )
            )
            return backend, spec

        _, success_spec = stage_and_submit(
            name="ssh-integration",
            command=["python", "runner.py"],
            outputs=["result.json"],
        )
        recovered = make_backend()
        deadline = time.monotonic() + 30
        current = recovered.status(success_spec.job_id or "", execute=True)
        while current.status in {JobStatus.SUBMITTED, JobStatus.RUNNING}:
            assert time.monotonic() < deadline
            time.sleep(0.1)
            current = recovered.status(success_spec.job_id or "", execute=True)
        assert current.status is JobStatus.SUCCEEDED, recovered.logs(
            success_spec.job_id or "",
            execute=True,
        ).stdout
        assert "ssh-integration-ok" in recovered.logs(
            success_spec.job_id or "",
            execute=True,
        ).stdout
        synced = recovered.sync_artifacts(
            success_spec.job_id or "",
            tmp_path / "synced",
            direction=ArtifactDirection.DOWNLOAD,
            execute=True,
        )
        assert synced.status is JobStatus.SUCCEEDED
        assert json.loads(
            (tmp_path / "synced" / "result.json").read_text(encoding="utf-8")
        ) == {"ok": True}
        _, repeated_binding = build_compute_binding(
            workspace,
            job_spec={
                "name": "ssh-integration",
                "job_id": "ssh-integration",
                "command": ["python", "runner.py"],
                "workdir": ".",
                "outputs": ["result.json"],
                "execute": True,
            },
            compute_backend=backend_kind,
            compute_config=binding_config,
        )
        repeated_stage = recovered.stage_source(
            success_spec,
            Path(str(repeated_binding["execution_worktree"])),
            execute=True,
        )
        assert repeated_stage.status is JobStatus.SUCCEEDED, repeated_stage.stderr

        _, retry_spec = stage_and_submit(
            name="ssh-retry",
            command=[
                "python",
                "-c",
                (
                    "from pathlib import Path; p=Path('result.json'); "
                    "assert not p.read_text(); p.write_text('fresh-attempt')"
                ),
            ],
            outputs=["result.json"],
        )
        assert _wait_for_ssh_terminal(
            backend,
            retry_spec.job_id or "",
        ) is JobStatus.SUCCEEDED
        resumed_retry = backend.resume(retry_spec.job_id or "", execute=True)
        assert resumed_retry.status is JobStatus.SUBMITTED, resumed_retry.stderr
        containers.append(
            backend._remote_container_name(
                retry_spec.job_id or "",
                int(resumed_retry.metadata["attempt"]),
            )
        )
        assert _wait_for_ssh_terminal(
            backend,
            retry_spec.job_id or "",
        ) is JobStatus.SUCCEEDED
        retry_sync = backend.sync_artifacts(
            retry_spec.job_id or "",
            tmp_path / "retry-synced",
            direction=ArtifactDirection.DOWNLOAD,
            execute=True,
        )
        assert retry_sync.status is JobStatus.SUCCEEDED, retry_sync.stderr
        assert retry_sync.artifacts[0].attempt_id == 2
        assert (tmp_path / "retry-synced" / "result.json").read_text(
            encoding="utf-8"
        ) == "fresh-attempt"

        _, mutation_spec = stage_and_submit(
            name="ssh-mutation",
            command=[
                "python",
                "-c",
                "from pathlib import Path; Path('runner.py').write_text('changed')",
            ],
        )
        assert _wait_for_ssh_terminal(backend, mutation_spec.job_id or "") is JobStatus.FAILED
        assert "ssh-integration-ok" in (workspace / "runner.py").read_text(
            encoding="utf-8"
        )

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port_number = int(listener.getsockname()[1])
        _, network_spec = stage_and_submit(
            name="ssh-network",
            command=[
                "python",
                "-c",
                (
                    "import socket; "
                    f"socket.create_connection(('127.0.0.1', {port_number}), 0.5)"
                ),
            ],
        )
        assert _wait_for_ssh_terminal(backend, network_spec.job_id or "") is JobStatus.FAILED

        _, timeout_spec = stage_and_submit(
            name="ssh-timeout",
            command=["python", "-c", "import time; time.sleep(30)"],
            resources={"timeout_seconds": 1},
        )
        timeout_result = _wait_for_ssh_result(backend, timeout_spec.job_id or "")
        assert timeout_result.status is JobStatus.FAILED
        assert timeout_result.return_code == 124
        assert timeout_result.metadata["timed_out"] is True
        timeout_container = backend._remote_container_name(
            timeout_spec.job_id or "",
            int(timeout_result.metadata["attempt"]),
        )
        inspect_timeout = subprocess.run(
            ["docker", "inspect", timeout_container],
            check=False,
            capture_output=True,
            text=True,
        )
        assert inspect_timeout.returncode != 0

        _, cancel_spec = stage_and_submit(
            name="ssh-cancel",
            command=["python", "-c", "import time; time.sleep(30)"],
        )
        assert backend.cancel(cancel_spec.job_id or "", execute=True).status is JobStatus.CANCELLED
        inspect = subprocess.run(
            [
                "docker",
                "ps",
                "--quiet",
                "--filter",
                f"name={backend._remote_container_name(cancel_spec.job_id or '', 1)}",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        assert not inspect.stdout.strip()
    finally:
        if listener is not None:
            listener.close()
        for container in containers:
            subprocess.run(
                ["docker", "rm", "--force", container],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        subprocess.run(
            ["docker", "rm", "--force", fixture_name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        shutil.rmtree(fixture_root, ignore_errors=True)


def _wait_for_ssh_terminal(backend: SSHBackend, job_id: str) -> JobStatus:
    return _wait_for_ssh_result(backend, job_id).status


def _wait_for_ssh_result(backend: SSHBackend, job_id: str) -> JobResult:
    deadline = time.monotonic() + 30
    current = backend.status(job_id, execute=True)
    while current.status in {JobStatus.SUBMITTED, JobStatus.RUNNING}:
        assert time.monotonic() < deadline
        time.sleep(0.1)
        current = backend.status(job_id, execute=True)
    return current


@pytest.mark.skipif(
    os.environ.get("PAPERFORGE_REAL_SLURM") != "1",
    reason="real Slurm integration gate is not enabled",
)
def test_real_slurm_lifecycle_and_isolation(tmp_path: Path) -> None:
    for executable in (
        "sbatch",
        "squeue",
        "sacct",
        "scancel",
        "scontrol",
        "singularity",
    ):
        assert shutil.which(executable), f"missing integration tool: {executable}"
    image = Path(
        os.environ.get("PAPERFORGE_SLURM_IMAGE", "/tmp/paperforge-python.sif")
    ).resolve(strict=True)
    assert image.is_file()
    config_payload = {
        "sbatch_executable": str(Path(shutil.which("sbatch") or "").resolve()),
        "squeue_executable": str(Path(shutil.which("squeue") or "").resolve()),
        "sacct_executable": str(Path(shutil.which("sacct") or "").resolve()),
        "scancel_executable": str(Path(shutil.which("scancel") or "").resolve()),
        "scontrol_executable": str(Path(shutil.which("scontrol") or "").resolve()),
        "container_runtime": str(
            Path(shutil.which("singularity") or "").resolve()
        ),
        "container_image": str(image),
    }
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "runner.py").write_text(
        "from pathlib import Path\n"
        "Path('result.json').write_text('{\"ok\": true}', encoding='utf-8')\n"
        "print('slurm-integration-ok')\n",
        encoding="utf-8",
    )

    def build_spec(
        *,
        name: str,
        command: list[str],
        outputs: list[str] | None = None,
        resources: dict[str, int] | None = None,
    ) -> tuple[JobSpec, SlurmConfig]:
        spec, binding = build_compute_binding(
            workspace,
            job_spec={
                "name": name,
                "job_id": name,
                "command": command,
                "workdir": ".",
                "outputs": outputs or [],
                "resources": resources or {},
                "execute": True,
            },
            compute_backend="slurm",
            compute_config=config_payload,
        )
        payload = spec.to_dict()
        payload["workdir"] = str(binding["execution_worktree"])
        return JobSpec.from_dict(payload), SlurmConfig(**binding["compute_config"])

    listener: socket.socket | None = None
    scheduler_ids: list[str] = []
    try:
        success_spec, config = build_spec(
            name="slurm-integration",
            command=["python", "runner.py"],
            outputs=["result.json"],
        )
        backend = SlurmBackend(
            config,
            policy=ExecutionPolicy(ExecutionProfile.FULL),
            state_dir=tmp_path / "state",
        )
        submitted = backend.submit(success_spec)
        assert submitted.status is JobStatus.SUBMITTED, submitted.stderr
        scheduler_ids.append(str(submitted.metadata["slurm_job_id"]))
        recovered = SlurmBackend(
            config,
            policy=ExecutionPolicy(ExecutionProfile.FULL),
            state_dir=tmp_path / "state",
        )
        assert _wait_for_slurm_terminal(
            recovered,
            success_spec.job_id or "",
        ) is JobStatus.SUCCEEDED
        assert "slurm-integration-ok" in recovered.logs(
            success_spec.job_id or "",
            execute=True,
        ).stdout
        synced = recovered.sync_artifacts(
            success_spec.job_id or "",
            tmp_path / "synced",
            direction=ArtifactDirection.DOWNLOAD,
            execute=True,
        )
        assert synced.status is JobStatus.SUCCEEDED
        assert json.loads(
            (tmp_path / "synced" / "result.json").read_text(encoding="utf-8")
        ) == {"ok": True}

        retry_spec, _ = build_spec(
            name="slurm-retry",
            command=[
                "python",
                "-c",
                (
                    "from pathlib import Path; p=Path('result.json'); "
                    "assert not p.read_text(); p.write_text('fresh-attempt')"
                ),
            ],
            outputs=["result.json"],
        )
        retry_submit = backend.submit(retry_spec)
        assert retry_submit.status is JobStatus.SUBMITTED, retry_submit.stderr
        scheduler_ids.append(str(retry_submit.metadata["slurm_job_id"]))
        assert _wait_for_slurm_terminal(
            backend,
            retry_spec.job_id or "",
        ) is JobStatus.SUCCEEDED
        retry_resume = backend.resume(retry_spec.job_id or "", execute=True)
        assert retry_resume.status is JobStatus.QUEUED, retry_resume.stderr
        scheduler_ids.append(str(retry_resume.metadata["slurm_job_id"]))
        assert _wait_for_slurm_terminal(
            backend,
            retry_spec.job_id or "",
        ) is JobStatus.SUCCEEDED
        retry_sync = backend.sync_artifacts(
            retry_spec.job_id or "",
            tmp_path / "retry-synced",
            direction=ArtifactDirection.DOWNLOAD,
            execute=True,
        )
        assert retry_sync.status is JobStatus.SUCCEEDED, retry_sync.stderr
        assert retry_sync.artifacts[0].attempt_id == 2
        assert (tmp_path / "retry-synced" / "result.json").read_text(
            encoding="utf-8"
        ) == "fresh-attempt"

        mutation_spec, _ = build_spec(
            name="slurm-mutation",
            command=[
                "python",
                "-c",
                "from pathlib import Path; Path('runner.py').write_text('changed')",
            ],
        )
        mutation = backend.submit(mutation_spec)
        assert mutation.status is JobStatus.SUBMITTED, mutation.stderr
        scheduler_ids.append(str(mutation.metadata["slurm_job_id"]))
        assert _wait_for_slurm_terminal(
            backend,
            mutation_spec.job_id or "",
        ) is JobStatus.FAILED
        assert "slurm-integration-ok" in (workspace / "runner.py").read_text(
            encoding="utf-8"
        )

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port_number = int(listener.getsockname()[1])
        network_spec, _ = build_spec(
            name="slurm-network",
            command=[
                "python",
                "-c",
                (
                    "import socket; "
                    f"socket.create_connection(('127.0.0.1', {port_number}), 0.5)"
                ),
            ],
        )
        network = backend.submit(network_spec)
        assert network.status is JobStatus.SUBMITTED, network.stderr
        scheduler_ids.append(str(network.metadata["slurm_job_id"]))
        assert _wait_for_slurm_terminal(
            backend,
            network_spec.job_id or "",
        ) is JobStatus.FAILED

        timeout_spec, _ = build_spec(
            name="slurm-timeout",
            command=["python", "-c", "import time; time.sleep(120)"],
            resources={"timeout_seconds": 1},
        )
        timeout = backend.submit(timeout_spec)
        assert timeout.status is JobStatus.SUBMITTED, timeout.stderr
        scheduler_ids.append(str(timeout.metadata["slurm_job_id"]))
        assert _wait_for_slurm_terminal(
            backend,
            timeout_spec.job_id or "",
        ) is JobStatus.FAILED

        cancel_spec, _ = build_spec(
            name="slurm-cancel",
            command=["python", "-c", "import time; time.sleep(30)"],
        )
        cancel_job = backend.submit(cancel_spec)
        assert cancel_job.status is JobStatus.SUBMITTED, cancel_job.stderr
        scheduler_ids.append(str(cancel_job.metadata["slurm_job_id"]))
        assert backend.cancel(
            cancel_spec.job_id or "",
            execute=True,
        ).status is JobStatus.CANCELLED
        resumed = backend.resume(cancel_spec.job_id or "", execute=True)
        assert resumed.status is JobStatus.QUEUED, resumed.stderr
        assert _wait_for_slurm_status(
            backend,
            cancel_spec.job_id or "",
            expected={JobStatus.QUEUED, JobStatus.RUNNING},
        ) in {JobStatus.QUEUED, JobStatus.RUNNING}
        assert backend.cancel(
            cancel_spec.job_id or "",
            execute=True,
        ).status is JobStatus.CANCELLED
    finally:
        if listener is not None:
            listener.close()
        for scheduler_id in scheduler_ids:
            subprocess.run(
                ["scancel", scheduler_id],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )


def _wait_for_slurm_terminal(backend: SlurmBackend, job_id: str) -> JobStatus:
    return _wait_for_slurm_status(
        backend,
        job_id,
        expected={JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED},
    )


def _wait_for_slurm_status(
    backend: SlurmBackend,
    job_id: str,
    *,
    expected: set[JobStatus],
) -> JobStatus:
    deadline = time.monotonic() + 180
    current = backend.status(job_id, execute=True)
    while current.status not in expected:
        assert current.status is not JobStatus.UNKNOWN, current.stderr
        assert time.monotonic() < deadline
        time.sleep(0.1)
        current = backend.status(job_id, execute=True)
    return current.status


@pytest.mark.skipif(
    os.environ.get("PAPERFORGE_REAL_KUBERNETES") != "1",
    reason="real Kubernetes integration gate is not enabled",
)
def test_real_kubernetes_lifecycle_and_isolation(tmp_path: Path) -> None:
    kubectl = Path(
        os.environ.get("PAPERFORGE_KUBECTL", shutil.which("kubectl") or "")
    ).resolve(strict=True)
    context = os.environ.get("PAPERFORGE_KUBERNETES_CONTEXT", "kind-paperforge-v3")
    namespace = os.environ.get("PAPERFORGE_KUBERNETES_NAMESPACE", "paperforge-v3")
    config_payload = {
        "image": _DOCKER_IMAGE,
        "namespace": namespace,
        "context": context,
        "kubectl_executable": str(kubectl),
        "image_pull_policy": "IfNotPresent",
        "ttl_seconds_after_finished": None,
        "source_pvc": "paperforge-source",
        "artifact_pvc": "paperforge-artifacts",
        "source_transport_image": _DOCKER_IMAGE,
    }
    cluster = subprocess.run(
        [str(kubectl), "--context", context, "cluster-info"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert cluster.returncode == 0, cluster.stderr
    canary_suffix = uuid.uuid4().hex[:12]
    canary_name = f"paperforge-canary-{canary_suffix}"
    control_name = f"paperforge-control-{canary_suffix}"
    canary_payload = {
        "apiVersion": "v1",
        "kind": "List",
        "items": [
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": canary_name, "namespace": namespace},
                "spec": {
                    "replicas": 1,
                    "selector": {"matchLabels": {"app": canary_name}},
                    "template": {
                        "metadata": {"labels": {"app": canary_name}},
                        "spec": {
                            "automountServiceAccountToken": False,
                            "containers": [
                                {
                                    "name": "server",
                                    "image": _DOCKER_IMAGE,
                                    "imagePullPolicy": "IfNotPresent",
                                        "command": [
                                            "python",
                                            "-m",
                                            "http.server",
                                            "8080",
                                        ],
                                        "readinessProbe": {
                                            "tcpSocket": {"port": 8080},
                                            "periodSeconds": 1,
                                            "failureThreshold": 30,
                                        },
                                        "securityContext": {
                                        "allowPrivilegeEscalation": False,
                                        "capabilities": {"drop": ["ALL"]},
                                        "readOnlyRootFilesystem": True,
                                        "runAsNonRoot": True,
                                        "runAsUser": 65532,
                                        "runAsGroup": 65532,
                                        "seccompProfile": {"type": "RuntimeDefault"},
                                    },
                                }
                            ],
                        },
                    },
                },
            },
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": canary_name, "namespace": namespace},
                "spec": {
                    "selector": {"app": canary_name},
                    "ports": [{"port": 8080, "targetPort": 8080}],
                },
            },
        ],
    }
    canary_apply = subprocess.run(
        [
            str(kubectl),
            "--context",
            context,
            "--namespace",
            namespace,
            "apply",
            "-f",
            "-",
        ],
        input=json.dumps(canary_payload),
        check=False,
        capture_output=True,
        text=True,
    )
    assert canary_apply.returncode == 0, canary_apply.stderr
    canary_ready = subprocess.run(
        [
            str(kubectl),
            "--context",
            context,
            "--namespace",
            namespace,
            "rollout",
            "status",
            f"deployment/{canary_name}",
            "--timeout=90s",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert canary_ready.returncode == 0, canary_ready.stderr
    canary_ip_result = subprocess.run(
        [
            str(kubectl),
            "--context",
            context,
            "--namespace",
            namespace,
            "get",
            "service",
            canary_name,
            "-o",
            "jsonpath={.spec.clusterIP}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    canary_ip = canary_ip_result.stdout.strip()
    assert canary_ip
    control_payload = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": control_name, "namespace": namespace},
        "spec": {
            "backoffLimit": 0,
            "template": {
                "metadata": {"labels": {"paperforge-network-control": canary_suffix}},
                "spec": {
                    "restartPolicy": "Never",
                    "automountServiceAccountToken": False,
                    "containers": [
                        {
                            "name": "control",
                            "image": _DOCKER_IMAGE,
                            "imagePullPolicy": "IfNotPresent",
                            "command": [
                                    "python",
                                    "-c",
                                    (
                                        "import socket, time\n"
                                        "deadline = time.monotonic() + 20\n"
                                        "while True:\n"
                                        "    try:\n"
                                        f"        socket.create_connection(('{canary_ip}', 8080), 2).close()\n"
                                        "        break\n"
                                        "    except OSError:\n"
                                        "        if time.monotonic() >= deadline:\n"
                                        "            raise\n"
                                        "        time.sleep(0.2)\n"
                                        "print('network-control-ok')\n"
                                    ),
                            ],
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "capabilities": {"drop": ["ALL"]},
                                "readOnlyRootFilesystem": True,
                                "runAsNonRoot": True,
                                "runAsUser": 65532,
                                "runAsGroup": 65532,
                                "seccompProfile": {"type": "RuntimeDefault"},
                            },
                        }
                    ],
                },
            },
        },
    }
    control_apply = subprocess.run(
        [
            str(kubectl),
            "--context",
            context,
            "--namespace",
            namespace,
            "apply",
            "-f",
            "-",
        ],
        input=json.dumps(control_payload),
        check=False,
        capture_output=True,
        text=True,
    )
    assert control_apply.returncode == 0, control_apply.stderr
    control_wait = subprocess.run(
        [
            str(kubectl),
            "--context",
            context,
            "--namespace",
            namespace,
            "wait",
            "--for=condition=complete",
            f"job/{control_name}",
            "--timeout=60s",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert control_wait.returncode == 0, control_wait.stderr
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "runner.py").write_text(
        "from pathlib import Path\n"
        "Path('result.json').write_text('{\"ok\": true}', encoding='utf-8')\n"
        "print('kubernetes-integration-ok')\n",
        encoding="utf-8",
    )

    def build_spec(
        *,
        name: str,
        command: list[str],
        outputs: list[str] | None = None,
        resources: dict[str, int] | None = None,
    ) -> tuple[JobSpec, KubernetesConfig, Path]:
        spec, binding = build_compute_binding(
            workspace,
            job_spec={
                "name": name,
                "job_id": name,
                "command": command,
                "workdir": ".",
                "outputs": outputs or [],
                "resources": resources or {},
                "execute": True,
            },
            compute_backend="kubernetes",
            compute_config=config_payload,
        )
        return (
            spec,
            KubernetesConfig(**binding["compute_config"]),
            Path(str(binding["execution_worktree"])),
        )

    backends: list[KubernetesBackend] = []
    try:
        success_spec, config, source_snapshot = build_spec(
            name="kubernetes-integration",
            command=["python", "runner.py"],
            outputs=["result.json"],
        )
        backend = KubernetesBackend(
            config,
            policy=ExecutionPolicy(ExecutionProfile.FULL),
            state_dir=tmp_path / "state",
        )
        backends.append(backend)
        staged = backend.stage_source(success_spec, source_snapshot, execute=True)
        assert staged.status is JobStatus.SUCCEEDED, staged.stderr
        submitted = backend.submit(success_spec)
        assert submitted.status is JobStatus.SUBMITTED, submitted.stderr
        recovered = KubernetesBackend(
            config,
            policy=ExecutionPolicy(ExecutionProfile.FULL),
            state_dir=tmp_path / "state",
        )
        backends.append(recovered)
        terminal = _wait_for_kubernetes_terminal(
            recovered,
            success_spec.job_id or "",
        )
        success_logs = recovered.logs(
            success_spec.job_id or "",
            execute=True,
        )
        assert terminal is JobStatus.SUCCEEDED, (
            success_logs.stderr or success_logs.stdout
        )
        assert "kubernetes-integration-ok" in recovered.logs(
            success_spec.job_id or "",
            execute=True,
        ).stdout
        synced = recovered.sync_artifacts(
            success_spec.job_id or "",
            tmp_path / "synced",
            direction=ArtifactDirection.DOWNLOAD,
            execute=True,
        )
        assert synced.status is JobStatus.SUCCEEDED, synced.stderr
        assert json.loads(
            (tmp_path / "synced" / "result.json").read_text(encoding="utf-8")
        ) == {"ok": True}

        retry_spec, _, retry_source = build_spec(
            name="kubernetes-retry",
            command=[
                "python",
                "-c",
                (
                    "from pathlib import Path; p=Path('result.json'); "
                    "assert not p.read_text(); p.write_text('fresh-attempt')"
                ),
            ],
            outputs=["result.json"],
        )
        retry_stage = backend.stage_source(retry_spec, retry_source, execute=True)
        assert retry_stage.status is JobStatus.SUCCEEDED, retry_stage.stderr
        retry_submit = backend.submit(retry_spec)
        assert retry_submit.status is JobStatus.SUBMITTED, retry_submit.stderr
        assert _wait_for_kubernetes_terminal(
            backend,
            retry_spec.job_id or "",
        ) is JobStatus.SUCCEEDED
        retry_resume = backend.resume(retry_spec.job_id or "", execute=True)
        assert retry_resume.status is JobStatus.SUBMITTED, retry_resume.stderr
        assert _wait_for_kubernetes_terminal(
            backend,
            retry_spec.job_id or "",
        ) is JobStatus.SUCCEEDED
        retry_sync = backend.sync_artifacts(
            retry_spec.job_id or "",
            tmp_path / "retry-synced",
            direction=ArtifactDirection.DOWNLOAD,
            execute=True,
        )
        assert retry_sync.status is JobStatus.SUCCEEDED, retry_sync.stderr
        assert retry_sync.artifacts[0].attempt_id == 2
        assert (tmp_path / "retry-synced" / "result.json").read_text(
            encoding="utf-8"
        ) == "fresh-attempt"

        mutation_spec, _, mutation_source = build_spec(
            name="kubernetes-mutation",
            command=[
                "python",
                "-c",
                "from pathlib import Path; Path('runner.py').write_text('changed')",
            ],
        )
        mutation_stage = backend.stage_source(
            mutation_spec,
            mutation_source,
            execute=True,
        )
        assert mutation_stage.status is JobStatus.SUCCEEDED, mutation_stage.stderr
        mutation = backend.submit(mutation_spec)
        assert mutation.status is JobStatus.SUBMITTED, mutation.stderr
        assert _wait_for_kubernetes_terminal(
            backend,
            mutation_spec.job_id or "",
        ) is JobStatus.FAILED
        assert "kubernetes-integration-ok" in (workspace / "runner.py").read_text(
            encoding="utf-8"
        )

        network_spec, _, network_source = build_spec(
            name="kubernetes-network",
            command=[
                "python",
                "-c",
                (
                    "import socket; "
                    f"socket.create_connection(('{canary_ip}', 8080), 2)"
                ),
            ],
        )
        network_stage = backend.stage_source(
            network_spec,
            network_source,
            execute=True,
        )
        assert network_stage.status is JobStatus.SUCCEEDED, network_stage.stderr
        network = backend.submit(network_spec)
        assert network.status is JobStatus.SUBMITTED, network.stderr
        assert _wait_for_kubernetes_terminal(
            backend,
            network_spec.job_id or "",
        ) is JobStatus.FAILED

        timeout_spec, _, timeout_source = build_spec(
            name="kubernetes-timeout",
            command=["python", "-c", "import time; time.sleep(30)"],
            resources={"timeout_seconds": 1},
        )
        timeout_stage = backend.stage_source(
            timeout_spec,
            timeout_source,
            execute=True,
        )
        assert timeout_stage.status is JobStatus.SUCCEEDED, timeout_stage.stderr
        timeout_submit = backend.submit(timeout_spec)
        assert timeout_submit.status is JobStatus.SUBMITTED, timeout_submit.stderr
        assert _wait_for_kubernetes_terminal(
            backend,
            timeout_spec.job_id or "",
        ) is JobStatus.FAILED

        cancel_spec, _, cancel_source = build_spec(
            name="kubernetes-cancel",
            command=["python", "-c", "import time; time.sleep(30)"],
        )
        cancel_stage = backend.stage_source(
            cancel_spec,
            cancel_source,
            execute=True,
        )
        assert cancel_stage.status is JobStatus.SUCCEEDED, cancel_stage.stderr
        cancel_job = backend.submit(cancel_spec)
        assert cancel_job.status is JobStatus.SUBMITTED, cancel_job.stderr
        assert backend.cancel(
            cancel_spec.job_id or "",
            execute=True,
        ).status is JobStatus.CANCELLED
        resumed = backend.resume(cancel_spec.job_id or "", execute=True)
        assert resumed.status is JobStatus.SUBMITTED, resumed.stderr
        assert _wait_for_kubernetes_status(
            backend,
            cancel_spec.job_id or "",
            expected={JobStatus.RUNNING},
        ) is JobStatus.RUNNING
        assert backend.cancel(
            cancel_spec.job_id or "",
            execute=True,
        ).status is JobStatus.CANCELLED
    finally:
        for backend in backends:
            for job_id in (
                "kubernetes-integration",
                "kubernetes-mutation",
                "kubernetes-retry",
                "kubernetes-network",
                "kubernetes-timeout",
                "kubernetes-cancel",
            ):
                with contextlib.suppress(KeyError):
                    backend.cancel(job_id, execute=True)
        subprocess.run(
            [
                str(kubectl),
                "--context",
                context,
                "--namespace",
                namespace,
                "delete",
                f"job/{control_name}",
                f"deployment/{canary_name}",
                f"service/{canary_name}",
                "--ignore-not-found=true",
                "--wait=true",
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def _wait_for_kubernetes_terminal(
    backend: KubernetesBackend,
    job_id: str,
) -> JobStatus:
    return _wait_for_kubernetes_status(
        backend,
        job_id,
        expected={JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED},
    )


def _wait_for_kubernetes_status(
    backend: KubernetesBackend,
    job_id: str,
    *,
    expected: set[JobStatus],
) -> JobStatus:
    deadline = time.monotonic() + 90
    current = backend.status(job_id, execute=True)
    while current.status not in expected:
        assert current.status is not JobStatus.UNKNOWN, current.stderr
        assert time.monotonic() < deadline
        time.sleep(0.2)
        current = backend.status(job_id, execute=True)
    return current.status
