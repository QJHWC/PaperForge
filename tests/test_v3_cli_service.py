from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import pytest

import paperforge.compute.docker as docker_module
from paperforge.api import PaperForgeService
from paperforge.artifacts import sha256_file
from paperforge.cli import main
from paperforge.compute import (
    CommandOutcome,
    DockerBackend,
    DockerConfig,
    build_compute_binding,
    verify_compute_binding,
)
from paperforge.experiments import ExperimentManager
from paperforge.models import (
    ClaimStatus,
    ClaimType,
    CompletionGate,
    ExecutionProfile,
    WorkflowStatus,
)
from paperforge.path_safety import UnsafePathError
from paperforge.publication import SourceBundler
from paperforge.publication.engine import (
    PUBLICATION_MANIFEST_SCHEMA,
    PublicationEngine,
)
from paperforge.publication.invariants import InvariantSnapshot
from paperforge.release import (
    ReleaseVerificationError,
    ReleaseVerifier,
    _extract_source_bundle,
    scan_workspace_secrets,
    write_page_inspection,
)
from paperforge.scientific_memory import ScientificMemory
from paperforge.workflow import InvalidTransition


def _staged_compute_inputs(
    full_job_spec: dict[str, object],
) -> dict[str, object]:
    full = dict(full_job_spec)
    metadata = dict(full.get("metadata") or {})
    metadata["estimated_cost"] = 0.5
    full["metadata"] = metadata
    return {
        "compute_backend": "local",
        "cost_limit": 1.0,
        "experiment_stages": {
            "static_check": {
                "compute_backend": "local",
                "compute_config": {},
                "job_spec": {
                    "name": "static-check",
                    "command": [sys.executable, "-c", "print('static')"],
                    "execute": True,
                    "resources": {"timeout_seconds": 60},
                    "metadata": {"estimated_cost": 0.1},
                },
            },
            "mini_experiment": {
                "compute_backend": "local",
                "compute_config": {},
                "job_spec": {
                    "name": "mini-check",
                    "command": [sys.executable, "-c", "print('mini')"],
                    "execute": True,
                    "resources": {"timeout_seconds": 60},
                    "metadata": {"estimated_cost": 0.2},
                },
            },
        },
        "job_spec": full,
    }


def test_cli_rejects_secret_flags_without_echoing_value(capsys) -> None:
    secret = "canary-not-for-output"
    with pytest.raises(SystemExit) as raised:
        main(["preflight", f"--openai-api-key={secret}"])
    assert secret not in str(raised.value)
    assert secret not in capsys.readouterr().out


def test_service_persists_and_resumes_writing_only(tmp_path: Path) -> None:
    service = PaperForgeService(tmp_path)
    handle = service.run(profile=ExecutionProfile.WRITING_ONLY)
    assert handle.status == WorkflowStatus.RUNNING.value
    assert handle.checkpoint == "runtime"
    assert handle.metadata["runtime_executed"] is False
    assert (tmp_path / handle.metadata["runtime_report"]).is_file()
    assert "paper" in handle.metadata["blocked_roles"]
    assert service.status(handle.run_id).run_id == handle.run_id

    service.workflow.transition(handle.run_id, WorkflowStatus.INTERRUPTED)
    resumed = service.resume(handle.run_id)
    assert resumed.status == WorkflowStatus.RUNNING.value
    assert resumed.metadata["runtime_executed"] is False


def test_writing_only_executes_provider_and_persists_guarded_draft(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "PAPERFORGE_CREDENTIAL_BAILU_PRIMARY",
        "fixture-provider-credential",
    )
    calls: list[dict[str, object]] = []
    response = (
        "\\documentclass{article}\n"
        "\\title{Evidence-Gated Writing}\n"
        "\\begin{document}\n"
        "\\maketitle\n"
        "\\begin{abstract}\n"
        "This draft describes an evidence-gated writing workflow.\n"
        "\\end{abstract}\n"
        "\\section{Method}\n"
        "The method separates drafting from publication approval.\n"
        "% PAPERFORGE-PROTECTED-EXPERIMENT-START\n"
        "% Author-supplied experimental setup and results remain unchanged.\n"
        "% PAPERFORGE-PROTECTED-EXPERIMENT-END\n"
        "\\section{Conclusion}\n"
        "The draft remains subject to the claim gate.\n"
        "\\bibliographystyle{plain}\n"
        "\\bibliography{references}\n"
        "\\end{document}\n"
    )

    def fake_request(model: str, **kwargs: object) -> str:
        calls.append({"model": model, **kwargs})
        return response

    monkeypatch.setattr("paperforge.runtime.chat_completion_text", fake_request)
    service = PaperForgeService(tmp_path)
    handle = service.run(
        profile=ExecutionProfile.WRITING_ONLY,
        inputs={
            "title": "Evidence-Gated Writing",
            "topic": "A safe scientific writing workflow",
        },
    )

    assert len(calls) == 1
    assert calls[0]["model"] == "bailu-turing"
    assert "paper" in handle.metadata["completed_roles"]
    assert "reviewer" in handle.metadata["blocked_roles"]
    assert handle.metadata["runtime_executed"] is False
    report = json.loads(
        (tmp_path / handle.metadata["runtime_report"]).read_text(
            encoding="utf-8"
        )
    )
    paper = next(
        item for item in report["agent_results"] if item["role"] == "paper"
    )
    draft = tmp_path / paper["output"]["draft"]["artifact"]["path"]
    assert draft.read_text(encoding="utf-8") == response
    assert paper["output"]["draft"]["protected_sha256"]


def test_writing_only_enters_auth_blocked_without_a_credential(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_home = tmp_path / "config-home"
    config_home.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    for name in (
        "OPENAI_API_KEY",
        "OPENAI_WRITEUP_API_KEY",
        "PAPERFORGE_CREDENTIAL_BAILU_PRIMARY",
    ):
        monkeypatch.delenv(name, raising=False)
    service = PaperForgeService(tmp_path / "workspace")

    handle = service.run(
        profile=ExecutionProfile.WRITING_ONLY,
        inputs={"topic": "Credential-gated draft"},
    )

    assert handle.status == WorkflowStatus.AUTH_BLOCKED.value
    assert handle.metadata["auth_blocked"] is True
    assert handle.metadata["provider_status"] == "AUTH_BLOCKED"
    assert "paper" in handle.metadata["blocked_roles"]


def test_terminal_agent_failure_never_marks_runtime_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_reviewer(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("terminal reviewer failure")

    monkeypatch.setattr(
        "paperforge.runtime.ResearchOSRuntime._handle_reviewer",
        fail_reviewer,
    )
    service = PaperForgeService(tmp_path)

    handle = service.run(profile=ExecutionProfile.RESEARCH)

    assert handle.metadata["runtime_executed"] is False
    assert handle.metadata["failed_roles"] == ["reviewer"]
    assert not handle.metadata["blocked_roles"]


def test_full_profile_waits_for_approval(tmp_path: Path) -> None:
    service = PaperForgeService(tmp_path)
    handle = service.run(profile=ExecutionProfile.FULL)
    assert handle.status == WorkflowStatus.AWAITING_APPROVAL.value
    proposal_id = handle.metadata["proposal_id"]
    with pytest.raises(PermissionError):
        service.resume(handle.run_id)
    service.approve(proposal_id)
    assert service.resume(handle.run_id).status == WorkflowStatus.RUNNING.value


def test_full_runtime_executes_all_agents_compute_and_cv_visualization(
    tmp_path: Path,
) -> None:
    service = PaperForgeService(tmp_path)
    evidence = service.memory.add_evidence(
        evidence_type="SOURCE_CODE",
        excerpt="verified runtime fixture",
    )
    claim = service.memory.add_claim(
        claim_type=ClaimType.STATIC_IMPLEMENTATION,
        text="The runtime fixture is evidence backed.",
        status=ClaimStatus.SUPPORTED_STATIC,
    )
    service.memory.link_claim(claim, evidence)
    pending = service.run(
        profile=ExecutionProfile.FULL,
        inputs={
            "compute_backend": "local",
            "job_spec": {
                "name": "runtime-fixture",
                "command": ["python", "-c", "print('fixture')"],
                "outputs": ["result.json"],
                "execute": False,
            },
            "domain_plugin": "cv",
            "observed_rows": [
                {"target": "cat", "prediction": "cat"},
                {"target": "dog", "prediction": "cat"},
            ],
        },
    )
    service.approve(
        str(pending.metadata["proposal_id"]),
        scope={"maximum_stage": "full"},
    )
    handle = service.resume(pending.run_id)

    assert set(handle.metadata["completed_roles"]) == {
        "research",
        "experiment",
        "code",
        "compute",
        "analysis",
        "visualization",
        "reviewer",
        "release",
    }
    assert handle.metadata["skipped_roles"] == ["paper"]
    report = json.loads(
        (tmp_path / handle.metadata["runtime_report"]).read_text(
            encoding="utf-8"
        )
    )
    compute = next(
        item for item in report["agent_results"] if item["role"] == "compute"
    )
    assert compute["output"]["job"]["executed"] is False
    assert compute["output"]["job"]["backend"] == "local"
    visual = next(
        item
        for item in report["agent_results"]
        if item["role"] == "visualization"
    )
    paths = {item["path"] for item in visual["output"]["artifacts"]}
    assert any(path.endswith("/figure.pdf") for path in paths)
    assert any(path.endswith("/figure.tex") for path in paths)
    assert any(path.endswith("/caption.txt") for path in paths)
    assert any(path.endswith("/source.manifest.json") for path in paths)
    pdf_path = tmp_path / next(
        path for path in paths if path.endswith("/figure.pdf")
    )
    assert pdf_path.read_bytes().startswith(b"%PDF-1.4")
    tex_path = tmp_path / next(
        path for path in paths if path.endswith("/figure.tex")
    )
    assert "\\includegraphics" in tex_path.read_text(encoding="utf-8")
    manifest_path = tmp_path / next(
        path for path in paths if path.endswith("/source.manifest.json")
    )
    visualization_manifest = json.loads(
        manifest_path.read_text(encoding="utf-8")
    )
    assert visualization_manifest["spec_sha256"]
    assert visualization_manifest["source_sha256"]
    assert all(
        artifact["metadata"]["workflow_id"] == handle.run_id
        for artifact in visualization_manifest["artifacts"]
    )


def test_compute_execution_requires_bound_full_approval(tmp_path: Path) -> None:
    service = PaperForgeService(tmp_path)
    marker = tmp_path / "results" / "approval-marker.txt"
    script = tmp_path / "run.py"
    script.write_text(
        "from pathlib import Path\n"
        "Path('results').mkdir(exist_ok=True)\n"
        "Path('results/approval-marker.txt').write_text('ok', encoding='utf-8')\n",
        encoding="utf-8",
    )
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir()
    (unsafe / "run.py").write_text(
        "raise RuntimeError('wrong script')\n",
        encoding="utf-8",
    )
    raw_spec = {
        "name": "approval-binding",
        "command": [sys.executable, "run.py"],
        "workdir": ".",
        "outputs": ["results/approval-marker.txt"],
        "execute": True,
    }
    pending = service.run(
        profile=ExecutionProfile.FULL,
        inputs=_staged_compute_inputs(raw_spec),
    )
    proposal_id = str(pending.metadata["proposal_id"])
    proposal = ExperimentManager(
        tmp_path,
        profile=ExecutionProfile.FULL,
        memory=service.memory,
    ).get_proposal(proposal_id)
    binding = proposal.metadata["compute_binding"]
    assert binding["worktree"] == str(tmp_path)
    assert binding["inputs"][0]["path"] == str(script)
    service.approve(proposal_id, scope={"maximum_stage": "full"})
    service.execute_experiment_stage(proposal_id, "static_check")
    service.execute_experiment_stage(proposal_id, "mini_experiment")
    previous = Path.cwd()
    try:
        os.chdir(unsafe)
        executed = service.resume(pending.run_id)
        for _ in range(100):
            if marker.exists():
                break
            time.sleep(0.01)
    finally:
        os.chdir(previous)

    assert marker.read_text(encoding="utf-8") == "ok"
    assert "compute" in executed.metadata["completed_roles"]


def test_full_resume_reuses_verified_run_and_skips_unrequested_visualization(
    tmp_path: Path,
) -> None:
    service = PaperForgeService(tmp_path)
    counter = tmp_path / "results" / "resume-count.txt"
    script = tmp_path / "run-once.py"
    script.write_text(
        "from pathlib import Path\n"
        "path = Path('results/resume-count.txt')\n"
        "path.parent.mkdir(exist_ok=True)\n"
        "with path.open('a', encoding='utf-8') as handle:\n"
        "    handle.write('run\\n')\n",
        encoding="utf-8",
    )
    pending = service.run(
        profile=ExecutionProfile.FULL,
        inputs=_staged_compute_inputs(
            {
                "name": "resume-once",
                "command": [sys.executable, "run-once.py"],
                "workdir": ".",
                "outputs": ["results/resume-count.txt"],
                "execute": True,
            }
        ),
    )
    proposal_id = str(pending.metadata["proposal_id"])
    service.approve(proposal_id, scope={"maximum_stage": "full"})

    premature = service.resume(pending.run_id)
    assert premature.metadata["runtime_executed"] is False
    assert "compute" in premature.metadata["blocked_roles"]
    assert not counter.exists()

    service.execute_experiment_stage(proposal_id, "static_check")
    service.execute_experiment_stage(proposal_id, "mini_experiment")
    first_full = service.resume(pending.run_id)
    assert counter.read_text(encoding="utf-8") == "run\n"
    assert first_full.metadata["runtime_executed"] is False
    assert first_full.metadata["skipped_roles"] == ["visualization", "paper"]
    assert "reviewer" in first_full.metadata["blocked_roles"]

    evidence = service.memory.add_evidence(
        evidence_type="SOURCE_CODE",
        excerpt="verified resume fixture",
    )
    claim = service.memory.add_claim(
        claim_type=ClaimType.STATIC_IMPLEMENTATION,
        text="The resume fixture is evidence backed.",
        status=ClaimStatus.SUPPORTED_STATIC,
    )
    service.memory.link_claim(claim, evidence)
    completed = service.resume(pending.run_id)
    assert completed.metadata["runtime_executed"] is True
    assert completed.metadata["skipped_roles"] == ["visualization", "paper"]
    assert not completed.metadata["blocked_roles"]
    assert counter.read_text(encoding="utf-8") == "run\n"
    report = json.loads(
        (tmp_path / completed.metadata["runtime_report"]).read_text(
            encoding="utf-8"
        )
    )
    compute = next(
        item for item in report["agent_results"] if item["role"] == "compute"
    )
    assert compute["output"]["job"]["reused"] is True

    service.resume(pending.run_id)
    assert counter.read_text(encoding="utf-8") == "run\n"


def test_remote_compute_resumes_terminal_job_and_records_verified_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_tool = tmp_path / "docker-fixture"
    runtime_tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    runtime_tool.chmod(0o700)
    image = "fixture@sha256:" + ("d" * 64)
    script = tmp_path / "remote.py"
    script.write_text("print('remote fixture')\n", encoding="utf-8")
    config_path = tmp_path / "config.json"
    config_path.write_text('{"fixture": true}\n', encoding="utf-8")
    data_path = tmp_path / "data.json"
    data_path.write_text('[{"sample": 1}]\n', encoding="utf-8")

    sync_attempts = 0
    container_id = "c" * 64

    class RemoteRunner:
        def run(
            self,
            argv,
            *,
            cwd=None,
            env=None,
            timeout=None,
        ) -> CommandOutcome:
            del cwd, env, timeout
            command = tuple(str(part) for part in argv)
            if "run" in command:
                return CommandOutcome(0, f"{container_id}\n", "")
            if "inspect" in command:
                for output in (
                    tmp_path / ".paperforge" / "compute"
                ).rglob("metrics.json"):
                    if "artifacts" in output.parts:
                        output.write_text(
                            '{"accuracy": 0.9}\n',
                            encoding="utf-8",
                        )
                return CommandOutcome(
                    0,
                    f"{container_id}|exited|0\n",
                    "",
                )
            if "logs" in command:
                return CommandOutcome(0, "accuracy=0.9\n", "")
            if "cp" in command:
                destination = Path(command[-1])
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(
                    '{"accuracy": 0.9}\n',
                    encoding="utf-8",
                )
                return CommandOutcome(0, "", "")
            raise AssertionError(f"unexpected Docker command: {command}")

    def create_fake_backend(name, config, **kwargs):
        assert name == "docker"
        return DockerBackend(
            config,
            runner=RemoteRunner(),
            **kwargs,
        )

    monkeypatch.setattr(
        "paperforge.runtime.create_backend",
        create_fake_backend,
    )
    original_copy = docker_module.copy_local_artifacts

    def flaky_copy(**kwargs):
        nonlocal sync_attempts
        sync_attempts += 1
        if sync_attempts == 1:
            raise OSError("temporary artifact failure")
        return original_copy(**kwargs)

    monkeypatch.setattr(docker_module, "copy_local_artifacts", flaky_copy)
    inputs = _staged_compute_inputs(
        {
            "name": "remote-lifecycle",
            "command": ["python", "remote.py"],
            "workdir": ".",
            "outputs": ["results/metrics.json"],
            "execute": True,
            "metadata": {
                "metrics_paths": ["results/metrics.json"],
                "code_paths": ["remote.py"],
                "config_paths": ["config.json"],
                "data_paths": ["data.json"],
            },
        }
    )
    inputs["compute_backend"] = "docker"
    inputs["compute_config"] = {
        "runtime": str(runtime_tool),
        "image": image,
    }
    service = PaperForgeService(tmp_path)
    pending = service.run(
        profile=ExecutionProfile.FULL,
        inputs=inputs,
    )
    proposal_id = str(pending.metadata["proposal_id"])
    service.approve(proposal_id, scope={"maximum_stage": "full"})
    service.execute_experiment_stage(proposal_id, "static_check")
    service.execute_experiment_stage(proposal_id, "mini_experiment")

    submitted = service.resume(pending.run_id)
    assert submitted.metadata["runtime_executed"] is False
    assert "compute" in submitted.metadata["blocked_roles"]

    artifact_blocked = service.resume(pending.run_id)
    assert "compute" in artifact_blocked.metadata["blocked_roles"], (
        artifact_blocked.metadata
    )
    with service.memory.connect() as db:
        budget_rows = db.execute(
            """
            SELECT status, amount FROM experiment_budget_events
            WHERE experiment_id = ? AND stage = 'FULL_EXPERIMENT'
            """,
            (proposal_id,),
        ).fetchall()
    assert [(row["status"], row["amount"]) for row in budget_rows] == [
        ("CHARGED", 0.5)
    ]

    finalized = service.resume(pending.run_id)
    assert "compute" in finalized.metadata["completed_roles"], (
        finalized.metadata,
        sync_attempts,
    )
    runs = ExperimentManager(
        tmp_path,
        profile=ExecutionProfile.FULL,
        memory=service.memory,
    ).list_runs(proposal_id)
    full_run = next(
        run for run in runs if run.stage.value == "FULL_EXPERIMENT"
    )
    assert full_run.eligible_for_claims is True
    assert full_run.metadata["execution_verified"] is True
    assert (tmp_path / "results" / "metrics.json").is_file()
    with service.memory.connect() as db:
        budget_rows = db.execute(
            """
            SELECT status, amount FROM experiment_budget_events
            WHERE experiment_id = ? AND stage = 'FULL_EXPERIMENT'
            """,
            (proposal_id,),
        ).fetchall()
    assert [(row["status"], row["amount"]) for row in budget_rows] == [
        ("CHARGED", 0.5)
    ]


def test_compute_binding_blocks_script_changes_after_approval(
    tmp_path: Path,
) -> None:
    service = PaperForgeService(tmp_path)
    script = tmp_path / "run.py"
    script.write_text("print('approved')\n", encoding="utf-8")
    pending = service.run(
        profile=ExecutionProfile.FULL,
        inputs=_staged_compute_inputs(
            {
                "name": "changed-script",
                "command": [sys.executable, "run.py"],
                "workdir": ".",
                "execute": True,
            }
        ),
    )
    service.approve(str(pending.metadata["proposal_id"]))
    script.write_text("print('changed')\n", encoding="utf-8")

    resumed = service.resume(pending.run_id)

    assert "compute" in resumed.metadata["blocked_roles"]


def test_compute_binding_hashes_backend_tools_and_restricts_mounts(
    tmp_path: Path,
) -> None:
    tool = tmp_path / "scheduler-tool"
    tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    tool.chmod(0o700)
    image = tmp_path / "fixture.sif"
    image.write_bytes(b"immutable-container-fixture")
    config = {
        key: str(tool)
        for key in (
            "sbatch_executable",
            "squeue_executable",
            "sacct_executable",
            "scancel_executable",
            "scontrol_executable",
        )
    }
    config["container_runtime"] = str(tool)
    config["container_image"] = str(image)
    _, binding = build_compute_binding(
        tmp_path,
        job_spec={
            "name": "backend-binding",
            "command": [sys.executable, "-c", "print('bound')"],
            "workdir": ".",
            "execute": True,
        },
        compute_backend="slurm",
        compute_config=config,
    )
    assert len(binding["compute_dependencies"]) == 7
    assert verify_compute_binding(tmp_path, binding) == (True, "verified")

    tool.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    assert verify_compute_binding(tmp_path, binding)[0] is False

    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(ValueError, match="must equal"):
        build_compute_binding(
            tmp_path,
            job_spec={
                "name": "unsafe-mount",
                "command": [sys.executable, "-c", "print('bound')"],
                "workdir": ".",
            },
            compute_backend="docker",
            compute_config={
                "image": "fixture:latest",
                "runtime": sys.executable,
                "workspace_mount": str(outside),
            },
        )


def test_docker_binding_mounts_approved_snapshot_read_only(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "docker-runtime"
    runtime.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    runtime.chmod(0o700)
    script = tmp_path / "run.py"
    script.write_text("print('bound')\n", encoding="utf-8")
    image = "fixture@sha256:" + ("a" * 64)
    spec, binding = build_compute_binding(
        tmp_path,
        job_spec={
            "name": "docker-snapshot",
            "command": ["python", "run.py"],
            "workdir": ".",
            "outputs": ["results/metrics.json"],
            "execute": True,
        },
        compute_backend="docker",
        compute_config={"runtime": str(runtime), "image": image},
    )
    snapshot = Path(str(binding["execution_worktree"]))
    assert snapshot.is_dir()
    assert (snapshot / "run.py").read_text(encoding="utf-8") == (
        "print('bound')\n"
    )
    config = dict(binding["compute_config"])
    config["workspace_mount"] = str(snapshot)
    backend = DockerBackend(DockerConfig(**config))
    planned = backend.submit(spec, execute=False)
    argv = planned.command

    assert argv[argv.index("--network") + 1] == "none"
    assert "--read-only" in argv
    assert (
        argv[argv.index("--cap-drop")],
        argv[argv.index("--cap-drop") + 1],
    ) == ("--cap-drop", "ALL")
    volumes = [
        argv[index + 1]
        for index, value in enumerate(argv)
        if value == "--volume"
    ]
    workspace_volume = next(
        volume for volume in volumes if volume.endswith(":/workspace:ro")
    )
    workspace_host = Path(workspace_volume.removesuffix(":/workspace:ro"))
    expected_workspace = (
        backend.state_dir
        / "docker"
        / planned.job_id
        / "attempts"
        / "1"
        / "workspace"
    )
    assert workspace_host == expected_workspace
    assert "/paperforge-outputs:rw,noexec,nosuid,nodev,size=64m" in argv
    artifact_volume = next(
        volume
        for volume in volumes
        if volume.endswith(":/paperforge-outputs/0:rw")
    )
    artifact_host = Path(
        artifact_volume.removesuffix(":/paperforge-outputs/0:rw")
    )
    assert artifact_host == (
        backend.state_dir
        / "docker"
        / planned.job_id
        / "attempts"
        / "1"
        / "artifacts"
        / "results"
        / "metrics.json"
    )
    assert verify_compute_binding(tmp_path, binding) == (True, "verified")


def test_service_rejects_nonexistent_approval_and_secret_inputs(
    tmp_path: Path,
) -> None:
    service = PaperForgeService(tmp_path)

    with pytest.raises(KeyError):
        service.approve("proposal-does-not-exist")
    with pytest.raises(ValueError, match="must not contain credentials"):
        service.run(
            profile=ExecutionProfile.WRITING_ONLY,
            inputs={"api_key": "secret-input-fixture"},
        )
    with pytest.raises(PermissionError, match="unsupported fields"):
        service.run(
            profile=ExecutionProfile.WRITING_ONLY,
            inputs={"alias_payload": [{"x": 1}]},
        )
    with pytest.raises(PermissionError, match="non-document path"):
        service.run(
            profile=ExecutionProfile.WRITING_ONLY,
            inputs={"document_path": "weights/no-extension"},
        )


def test_scientific_memory_accepts_standard_macos_var_alias() -> None:
    with tempfile.TemporaryDirectory() as directory:
        memory = ScientificMemory(Path(directory) / "memory.db")
        assert memory.path.is_file()


def test_service_rejects_symlinked_state_directory(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    try:
        (workspace / ".paperforge").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")

    with pytest.raises(UnsafePathError, match="symbolic link"):
        PaperForgeService(workspace)

    assert list(outside.iterdir()) == []


def test_preflight_emits_machine_readable_status(tmp_path: Path, capsys) -> None:
    assert main(["preflight", "--workspace", str(tmp_path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "CODE_VERIFIED"


def test_cli_job_manifest_creates_and_approves_complete_binding(
    tmp_path: Path,
    capsys,
) -> None:
    manifest = tmp_path / "job.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": "paperforge.compute-job/v1",
                "compute_backend": "local",
                "compute_config": {},
                "job_spec": {
                    "name": "cli-binding",
                    "command": [sys.executable, "-c", "print('dry run')"],
                    "workdir": ".",
                    "execute": False,
                },
            }
        ),
        encoding="utf-8",
    )
    assert (
        main(
            [
                "run",
                "--profile",
                "full",
                "--workspace",
                str(tmp_path),
                "--job-manifest",
                str(manifest),
            ]
        )
        == 0
    )
    pending = json.loads(capsys.readouterr().out)
    proposal_id = pending["metadata"]["proposal_id"]
    assert pending["status"] == WorkflowStatus.AWAITING_APPROVAL.value

    assert (
        main(
            [
                "approve",
                "--workspace",
                str(tmp_path),
                "--proposal-id",
                proposal_id,
                "--scope",
                "full",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        main(
            [
                "resume",
                "--workspace",
                str(tmp_path),
                "--run-id",
                pending["run_id"],
            ]
        )
        == 0
    )
    resumed = json.loads(capsys.readouterr().out)
    assert "compute" in resumed["metadata"]["completed_roles"]


@pytest.mark.parametrize(
    "command_prefix",
    [
        ["run", "--profile", "writing-only"],
        ["writeup"],
    ],
)
def test_cli_accepts_real_writing_requests(
    tmp_path: Path,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
    command_prefix: list[str],
) -> None:
    monkeypatch.setenv(
        "PAPERFORGE_CREDENTIAL_BAILU_PRIMARY",
        "fixture-provider-credential",
    )
    response = (
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        "The draft is evidence gated.\n"
        "% PAPERFORGE-PROTECTED-EXPERIMENT-START\n"
        "% Author-supplied experimental setup and results remain unchanged.\n"
        "% PAPERFORGE-PROTECTED-EXPERIMENT-END\n"
        "\\bibliographystyle{plain}\n"
        "\\bibliography{references}\n"
        "\\end{document}\n"
    )
    monkeypatch.setattr(
        "paperforge.runtime.chat_completion_text",
        lambda *_args, **_kwargs: response,
    )

    assert (
        main(
            [
                *command_prefix,
                "--workspace",
                str(tmp_path),
                "--topic",
                "Evidence-gated paper writing",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)

    assert "paper" in result["metadata"]["completed_roles"]
    assert next((tmp_path / ".paperforge" / "drafts").glob("*.tex")).is_file()


def test_release_recomputes_authoritative_gate_and_rejects_caller_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = PaperForgeService(tmp_path)
    handle = service.run(profile=ExecutionProfile.WRITING_ONLY)
    (tmp_path / "main.tex").write_text(
        "\\documentclass{article}\n"
        "\\begin{document}\nVerified statement.\n"
        "\\bibliographystyle{plain}\n"
        "\\bibliography{references}\n"
        "\\end{document}\n",
        encoding="utf-8",
    )
    (tmp_path / "references.bib").write_text("", encoding="utf-8")
    source = service.memory.add_source(kind="TEST", uri="fixture://release")
    evidence = service.memory.add_evidence(
        evidence_type="SOURCE_CODE",
        source_id=source,
        excerpt="verified statement",
    )
    claim = service.memory.add_claim(
        claim_type=ClaimType.STATIC_IMPLEMENTATION,
        text="Verified statement.",
        status=ClaimStatus.SUPPORTED_STATIC,
        metadata={"tex_span": {"file": "main.tex", "start": 3, "end": 3}},
    )
    service.memory.link_claim(claim, evidence)

    bundle = SourceBundler().build(
        tmp_path,
        tmp_path / "dist" / "paper-source.zip",
    )
    pdf = tmp_path / "main.pdf"
    pdf.write_bytes(b"%PDF-1.4 fixture")
    page = tmp_path / ".paperforge" / "publication" / "page-1.png"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_bytes(b"png")
    claim_gate = service.memory.claim_gate()
    claim_manifest = dict(service.memory.claim_manifest({}))
    claim_manifest.pop("generated_at", None)
    coverage = PublicationEngine._claim_coverage(
        tmp_path / "main.tex",
        claim_manifest,
    )
    claim_manifest["coverage"] = coverage
    claim_gate["coverage"] = coverage
    spans = {
        item["claim_id"]: dict(item["tex_span"])
        for item in claim_manifest["claims"]
    }
    snapshot = InvariantSnapshot.capture(
        (tmp_path / "main.tex").read_text(encoding="utf-8"),
        scientific_memory=service.memory,
        claim_spans=spans,
    )
    claim_manifest_sha256 = hashlib.sha256(
        json.dumps(
            claim_manifest,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    gates = {
        "claim_gate": claim_gate,
        "compile": False,
        "render": False,
        "diagnostics": False,
        "invariants": False,
        "source_lock": False,
    }
    manifest = {
        "schema": PUBLICATION_MANIFEST_SCHEMA,
        "status": "passed",
        "project_root": ".",
        "profile": "generic",
        "entrypoint": "main.tex",
        "bibliography": "references.bib",
        "claim_manifest": claim_manifest,
        "source_invariants": {
            "entrypoint_sha256": sha256_file(tmp_path / "main.tex"),
            "bibliography_sha256": sha256_file(
                tmp_path / "references.bib"
            ),
            "claim_manifest_sha256": claim_manifest_sha256,
            **snapshot.fingerprint(),
        },
        "gates": gates,
        "rounds": [
            {
                "render": {
                    "success": True,
                    "page_count": 1,
                    "rendered_pages": [str(page)],
                }
            }
        ],
        "artifacts": {
            "pdf": {"path": "main.pdf", "sha256": sha256_file(pdf)},
            "source_bundle": {
                "path": "dist/paper-source.zip",
                "sha256": bundle.sha256,
                "checksum_path": str(
                    bundle.checksum_path.relative_to(tmp_path)
                ),
                "source_lock_path": str(
                    bundle.source_lock_path.relative_to(tmp_path)
                ),
                "source_lock_sha256": bundle.source_lock_sha256,
            },
        },
    }
    (tmp_path / "dist" / "publication.manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    write_page_inspection(
        tmp_path,
        pdf_path=pdf,
        rendered_pages=(page,),
        reviewer="test reviewer",
        inspection_kind="human",
        review_evidence={
            "pages_reviewed": [1],
            "checks": ["content", "cropping", "references"],
        },
    )

    with pytest.raises(InvalidTransition, match="caller-supplied"):
        service.release(run_id=handle.run_id, gate=CompletionGate())

    monkeypatch.setattr(
        ReleaseVerifier,
        "_revalidate_publication",
        lambda *args, **kwargs: {
            "passed": False,
            "official_pages_match_inspection": False,
        },
    )
    with pytest.raises(InvalidTransition):
        service.release(run_id=handle.run_id)

    monkeypatch.setattr(
        ReleaseVerifier,
        "_revalidate_publication",
        lambda *args, **kwargs: {
            "passed": True,
            "bundle_source_lock_matches_external": True,
            "official_pages_match_inspection": True,
            "rebuilt_pages_match_official": True,
        },
    )
    released = service.release(run_id=handle.run_id)
    assert released.status == WorkflowStatus.COMPLETED.value
    report = json.loads(
        (tmp_path / ".paperforge" / "release_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert report["gate"]["release_manifest_verified"]


def test_source_bundle_extraction_rejects_duplicate_casefold_paths(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("source.tex", "safe")
        archive.writestr("SOURCE.TEX", "duplicate")

    with pytest.raises(ReleaseVerificationError, match="duplicate"):
        _extract_source_bundle(bundle, tmp_path / "output")


def test_secret_scan_reads_large_files_and_reachable_git_history(
    tmp_path: Path,
) -> None:
    large_root = tmp_path / "large"
    large_root.mkdir()
    secret = "sk-" + ("x" * 24)
    large_file = large_root / ".env"
    with large_file.open("wb") as handle:
        handle.write(secret.encode("ascii"))
        handle.truncate(11 * 1024 * 1024)
    large_scan = scan_workspace_secrets(large_root)
    assert not large_scan["clean"]
    assert large_scan["findings"][0]["path"] == ".env"

    repository = tmp_path / "history"
    repository.mkdir()
    subprocess.run(("git", "init", "-q", str(repository)), check=True)
    subprocess.run(
        ("git", "-C", str(repository), "config", "user.name", "PaperForge Test"),
        check=True,
    )
    subprocess.run(
        (
            "git",
            "-C",
            str(repository),
            "config",
            "user.email",
            "paperforge@example.invalid",
        ),
        check=True,
    )
    historical = repository / "historical.txt"
    historical.write_text(secret, encoding="utf-8")
    subprocess.run(
        ("git", "-C", str(repository), "add", "historical.txt"),
        check=True,
    )
    subprocess.run(
        ("git", "-C", str(repository), "commit", "-q", "-m", "fixture"),
        check=True,
    )
    historical.unlink()
    subprocess.run(
        ("git", "-C", str(repository), "add", "-u"),
        check=True,
    )
    subprocess.run(
        ("git", "-C", str(repository), "commit", "-q", "-m", "remove fixture"),
        check=True,
    )

    history_scan = scan_workspace_secrets(repository)

    assert not history_scan["clean"]
    assert history_scan["scanned_git_blobs"] > 0
    assert any(
        finding["path"].startswith("git:")
        for finding in history_scan["findings"]
    )


def test_release_revalidation_binds_internal_and_external_source_locks(
    tmp_path: Path,
) -> None:
    project = tmp_path / "paper"
    project.mkdir()
    (project / "main.tex").write_text(
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        "Bound source.\n"
        "\\end{document}\n",
        encoding="utf-8",
    )
    (project / "references.bib").write_text("", encoding="utf-8")
    bundle = SourceBundler().build(
        project,
        tmp_path / "source.zip",
    )
    tampered = tmp_path / "tampered.zip"
    with (
        zipfile.ZipFile(bundle.bundle_path) as source,
        zipfile.ZipFile(tampered, "w") as destination,
    ):
        for member in source.infolist():
            content = source.read(member)
            if member.filename == "publication.source.lock.json":
                content += b" "
            destination.writestr(member, content)
    verifier = ReleaseVerifier(tmp_path)

    result = verifier._revalidate_publication(
        {"profile": "generic", "entrypoint": "main.tex"},
        official_pdf=tmp_path / "not-used.pdf",
        source_bundle=tampered,
        external_source_lock=bundle.source_lock_path,
        expected_source_lock_sha256=bundle.source_lock_sha256,
        inspected_pages=(),
    )

    assert not result["passed"]
    assert not result["bundle_source_lock_matches_external"]


def test_release_persistence_rejects_secret_canaries(tmp_path: Path) -> None:
    canary = "sk-" + ("x" * 24)
    with pytest.raises(
        ReleaseVerificationError,
        match="must not contain credentials",
    ):
        write_page_inspection(
            tmp_path,
            pdf_path=tmp_path / "paper.pdf",
            rendered_pages=(tmp_path / "page.png",),
            reviewer=canary,
            inspection_kind="human",
        )
    with pytest.raises(
        ReleaseVerificationError,
        match="must not contain credentials",
    ):
        ReleaseVerifier(tmp_path).write_report(
            CompletionGate(details={"diagnostic": canary})
        )
