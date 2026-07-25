from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agents.runtime import AgentBridgeResult, execute_command, planned_command
from engine.remote_runner import RemoteRunner, load_remote_config
from engine.secret_redaction import redact_command, redact_structure
from paperforge.policy import Action, PolicyViolation


class _FakeSFTP:
    def __init__(self) -> None:
        self.uploaded: list[tuple[str, str]] = []

    def stat(self, _path: str) -> object:
        return object()

    def mkdir(self, _path: str) -> None:
        return None

    def put(self, local_path: str, remote_path: str) -> None:
        self.uploaded.append((local_path, remote_path))


def _remote_config(known_hosts: Path) -> dict[str, Any]:
    return {
        "host": "compute.example",
        "port": 22,
        "username": "paperforge",
        "known_hosts_file": str(known_hosts),
        "auth": {"method": "key", "key_path": "~/.ssh/id_ed25519"},
        "remote_workdir": "/home/paperforge/experiment",
        "upload_paths": [],
        "upload_excludes": [],
        "train_command": "python train.py",
        "results_dir": "/home/paperforge/experiment/outputs",
    }


def test_remote_config_uses_non_root_and_pinned_host_defaults(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "remote.yaml"
    config_path.write_text(
        "\n".join(
            (
                "host: compute.example",
                "train_command: python train.py",
                "results_dir: /srv/results",
            )
        ),
        encoding="utf-8",
    )

    config = load_remote_config(str(config_path))

    assert config["username"] == "paperforge"
    assert config["remote_workdir"] == "/home/paperforge/experiment"
    assert config["known_hosts_file"] == "~/.ssh/known_hosts"


def test_remote_connect_loads_pinned_hosts_and_rejects_unknown_hosts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text(
        "compute.example ssh-ed25519 fixture\n",
        encoding="utf-8",
    )

    class RejectPolicy:
        pass

    class Client:
        def __init__(self) -> None:
            self.loaded: str | None = None
            self.policy: object | None = None
            self.kwargs: dict[str, Any] | None = None

        def load_host_keys(self, path: str) -> None:
            self.loaded = path

        def set_missing_host_key_policy(self, policy: object) -> None:
            self.policy = policy

        def connect(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        def open_sftp(self) -> object:
            return object()

    client = Client()
    fake_paramiko = SimpleNamespace(
        RejectPolicy=RejectPolicy,
        SSHClient=lambda: client,
    )
    monkeypatch.setattr(
        "engine.remote_runner._lazy_import_paramiko",
        lambda: fake_paramiko,
    )

    config = _remote_config(known_hosts)
    config["execution_profile"] = "full"
    runner = RemoteRunner(config)
    runner.connect()

    assert client.loaded == str(known_hosts.resolve())
    assert isinstance(client.policy, RejectPolicy)
    assert client.kwargs is not None
    assert client.kwargs["username"] == "paperforge"


def test_upload_denylist_cannot_be_overridden_and_contains_symlinks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("fixture", encoding="utf-8")
    upload_root = tmp_path / "payload"
    upload_root.mkdir()
    (upload_root / "safe.py").write_text("print('safe')", encoding="utf-8")
    (upload_root / ".env").write_text("fixture", encoding="utf-8")
    (upload_root / "key.sh.bak").write_text("fixture", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("fixture", encoding="utf-8")
    (upload_root / "innocent.txt").symlink_to(outside)

    fake_paramiko = SimpleNamespace()
    monkeypatch.setattr(
        "engine.remote_runner._lazy_import_paramiko",
        lambda: fake_paramiko,
    )
    config = _remote_config(known_hosts)
    config["upload_paths"] = [str(upload_root), str(upload_root / ".env")]
    config["upload_excludes"] = []
    runner = RemoteRunner(config)
    sftp = _FakeSFTP()
    runner._sftp = sftp

    assert runner.upload() == 1
    assert [
        Path(local).name for local, _remote in sftp.uploaded
    ] == ["safe.py"]


def test_remote_command_redacts_command_and_cross_chunk_output(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("fixture", encoding="utf-8")
    secret = "fixture-sensitive-value"
    monkeypatch.setenv("VENDOR_API_KEY", secret)
    monkeypatch.setattr(
        "engine.remote_runner._lazy_import_paramiko",
        lambda: SimpleNamespace(),
    )
    monkeypatch.setattr("engine.remote_runner.time.sleep", lambda _delay: None)

    class Channel:
        def __init__(self) -> None:
            self.chunks = [b"fixture-sensitive-", b"value\n"]

        def set_combine_stderr(self, _enabled: bool) -> None:
            return None

        def exec_command(self, command: str) -> None:
            assert command.endswith(secret)

        def recv_ready(self) -> bool:
            return bool(self.chunks)

        def recv(self, _size: int) -> bytes:
            return self.chunks.pop(0)

        def exit_status_ready(self) -> bool:
            return not self.chunks

        def recv_exit_status(self) -> int:
            return 0

        def close(self) -> None:
            return None

    channel = Channel()
    config = _remote_config(known_hosts)
    config["execution_profile"] = "full"
    runner = RemoteRunner(config)
    runner._client = SimpleNamespace(
        get_transport=lambda: SimpleNamespace(open_session=lambda: channel)
    )

    assert runner.run_command(f"tool --api-key {secret}") == 0
    captured = capsys.readouterr()
    assert secret not in captured.out
    assert "***redacted***" in captured.out


def test_full_cycle_checks_remote_policy_before_upload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("fixture", encoding="utf-8")
    monkeypatch.setattr(
        "engine.remote_runner._lazy_import_paramiko",
        lambda: SimpleNamespace(),
    )
    config = _remote_config(known_hosts)
    config["execution_profile"] = "writing-only"
    runner = RemoteRunner(config)
    uploaded = False

    def upload() -> int:
        nonlocal uploaded
        uploaded = True
        return 0

    monkeypatch.setattr(runner, "upload", upload)

    with pytest.raises(PolicyViolation):
        runner.run_full_cycle(str(tmp_path / "downloads"))
    assert not uploaded


def test_structured_and_command_redaction_covers_positional_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VENDOR_API_KEY", "fixture-sensitive-value")
    command = [
        "tool",
        "--api-key",
        "fixture-sensitive-value",
        "--password=another-fixture-value",
    ]
    structured = {
        "command": command,
        "stdout": "token=fixture-sensitive-value",
        "metadata": {
            "password": "another-fixture-value",
            "nested": ["Authorization: Bearer third-fixture-value"],
        },
    }

    safe_command = redact_command(command)
    safe_structure = redact_structure(structured)

    assert safe_command[2] == "***redacted***"
    assert safe_command[3] == "--password=***redacted***"
    rendered = repr(safe_structure)
    for value in (
        "fixture-sensitive-value",
        "another-fixture-value",
        "third-fixture-value",
    ):
        assert value not in rendered


def test_structured_secret_fields_redact_matching_unlabelled_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "opaque-fixture-value"
    monkeypatch.delenv("VENDOR_API_KEY", raising=False)
    structured = {
        "credentials": {"clientSecret": secret},
        "command": ["tool", secret],
        "detail": secret,
        Path(secret): "path-like mapping key",
    }

    rendered = repr(redact_structure(structured))

    assert secret not in rendered


def test_agent_result_and_completed_process_never_return_raw_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "fixture-sensitive-value"
    monkeypatch.setenv("VENDOR_API_KEY", secret)
    raw_command = ["tool", "--api-key", secret]

    def fake_run(
        *args: Any,
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=f"api_key={secret}",
            stderr=f"Authorization: Bearer {secret}",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    completed = execute_command(raw_command, profile="full")
    result = AgentBridgeResult(
        agent="fixture",
        entrypoint="fixture.py",
        status="completed",
        input_schema={},
        input={"access_token": secret},
        command=raw_command,
        trace=[{"stage": "execute", "detail": f"token={secret}"}],
        stdout=f"api_key={secret}",
        stderr=f"Authorization: Bearer {secret}",
    ).to_dict()

    assert secret not in repr(completed)
    assert secret not in repr(result)
    assert secret not in planned_command(raw_command)


def test_completed_process_redacts_secrets_from_explicit_child_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "child-only-sensitive-value"
    monkeypatch.delenv("CHILD_API_KEY", raising=False)

    def fake_run(
        *_args: Any,
        **_kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=secret,
            stderr=secret,
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    completed = execute_command(
        ["tool"],
        env={"CHILD_API_KEY": secret},
        profile="full",
    )

    assert secret not in repr(completed)


def test_process_execution_is_blocked_by_action_profile_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def fake_run(
        *_args: Any,
        **_kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal called
        called = True
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(PolicyViolation):
        execute_command(
            ["python", "experiment.py"],
            profile="writing-only",
        )
    assert not called


def test_process_execution_rejects_unbound_writing_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def fake_run(
        *_args: Any,
        **_kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal called
        called = True
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="ok",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(PolicyViolation, match="unbound subprocess"):
        execute_command(
            ["python", "write_draft.py"],
            action=Action.DRAFT_EDIT,
            profile="writing-only",
        )

    assert not called
