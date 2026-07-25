from __future__ import annotations

import os
import re
import shlex
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from paperforge.policy import Action

from ._artifacts import artifact_patterns
from .base import ComputeBackend
from .contracts import ArtifactDirection, JobResult, JobSpec, JobStatus


class SSHSecurityError(ValueError):
    pass


_HOST_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:-]{0,252}$")
_USER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
_EXECUTABLE_PATTERN = re.compile(r"^[A-Za-z0-9_./-]+$")
_SECRET_UPLOAD_PATTERN = re.compile(
    r"(?:^|[._-])(?:credential|identity|private|secret|token|key)(?:[._-]|$)",
    re.IGNORECASE,
)


def _validate_regular_file(
    path: Path,
    *,
    label: str,
    private: bool,
) -> Path:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise SSHSecurityError(f"{label} must not be a symbolic link")
    resolved = expanded.resolve()
    if not resolved.is_file():
        raise SSHSecurityError(f"{label} file does not exist: {resolved}")
    if resolved.stat().st_size == 0:
        raise SSHSecurityError(f"{label} file cannot be empty")
    mode = stat.S_IMODE(resolved.stat().st_mode)
    if mode & stat.S_IWGRP or mode & stat.S_IWOTH:
        raise SSHSecurityError(f"{label} has unsafe write permissions: {mode:o}")
    if private and mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise SSHSecurityError(f"{label} permissions must deny all group/other access: {mode:o}")
    return resolved


@dataclass(frozen=True)
class SSHConfig:
    host: str
    user: str
    known_hosts_file: str | Path
    identity_file: str | Path | None = None
    port: int = 22
    strict_host_key_checking: bool = True
    connect_timeout_seconds: int = 15
    ssh_executable: str = "ssh"
    scp_executable: str = "scp"
    remote_root: str = ".paperforge/jobs"

    def __post_init__(self) -> None:
        if not _HOST_PATTERN.fullmatch(self.host) or "@" in self.host:
            raise SSHSecurityError("SSH host contains unsafe characters")
        if not _USER_PATTERN.fullmatch(self.user):
            raise SSHSecurityError("SSH user contains unsafe characters")
        if self.user.casefold() == "root":
            raise SSHSecurityError("SSH root login is forbidden by the safe contract")
        if isinstance(self.port, bool) or not 1 <= self.port <= 65535:
            raise SSHSecurityError("SSH port must be between 1 and 65535")
        if not self.strict_host_key_checking:
            raise SSHSecurityError(
                "SSH host key verification cannot be disabled by the safe contract"
            )
        if (
            isinstance(self.connect_timeout_seconds, bool)
            or self.connect_timeout_seconds < 1
            or self.connect_timeout_seconds > 300
        ):
            raise SSHSecurityError("SSH connect timeout must be between 1 and 300 seconds")
        if not _EXECUTABLE_PATTERN.fullmatch(self.ssh_executable):
            raise SSHSecurityError("ssh_executable contains unsafe characters")
        if not _EXECUTABLE_PATTERN.fullmatch(self.scp_executable):
            raise SSHSecurityError("scp_executable contains unsafe characters")
        root = PurePosixPath(self.remote_root)
        if root.is_absolute() or ".." in root.parts or "\x00" in self.remote_root:
            raise SSHSecurityError(
                "remote_root must be a traversal-free path relative to the remote home"
            )

        known_hosts = _validate_regular_file(
            Path(self.known_hosts_file),
            label="known_hosts",
            private=False,
        )
        identity = None
        if self.identity_file is not None:
            identity = _validate_regular_file(
                Path(self.identity_file),
                label="identity file",
                private=True,
            )
        object.__setattr__(self, "known_hosts_file", known_hosts)
        object.__setattr__(self, "identity_file", identity)
        object.__setattr__(self, "remote_root", root.as_posix())

    @property
    def target(self) -> str:
        return f"{self.user}@{self.host}"

    def _security_options(self) -> tuple[str, ...]:
        options: list[str] = [
            "-o",
            "BatchMode=yes",
            "-o",
            "ForwardAgent=no",
            "-o",
            "ClearAllForwardings=yes",
            "-o",
            "PermitLocalCommand=no",
            "-o",
            "PasswordAuthentication=no",
            "-o",
            "KbdInteractiveAuthentication=no",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={Path(self.known_hosts_file).resolve()}",
            "-o",
            f"ConnectTimeout={self.connect_timeout_seconds}",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=3",
            "-o",
            "LogLevel=ERROR",
        ]
        if self.identity_file is not None:
            options.extend(
                [
                    "-i",
                    os.fspath(Path(self.identity_file).resolve()),
                    "-o",
                    "IdentitiesOnly=yes",
                ]
            )
        return tuple(options)

    def base_argv(self) -> tuple[str, ...]:
        return (
            self.ssh_executable,
            "-p",
            str(self.port),
            *self._security_options(),
        )

    def scp_base_argv(self) -> tuple[str, ...]:
        return (
            self.scp_executable,
            "-P",
            str(self.port),
            *self._security_options(),
        )

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "host": self.host,
            "user": self.user,
            "port": self.port,
            "known_hosts_file": str(self.known_hosts_file),
            "identity_file": str(self.identity_file) if self.identity_file else None,
            "strict_host_key_checking": True,
            "connect_timeout_seconds": self.connect_timeout_seconds,
            "remote_root": self.remote_root,
        }


class SSHBackend(ComputeBackend):
    name = "ssh"
    policy_action = Action.REMOTE_EXECUTE

    def __init__(self, config: SSHConfig, **kwargs: Any) -> None:
        self.config = config
        super().__init__(**kwargs)

    def _remote_dir(self, job_id: str) -> PurePosixPath:
        return PurePosixPath(self.config.remote_root) / job_id

    def _ssh_argv(self, remote_script: str) -> tuple[str, ...]:
        if "\x00" in remote_script:
            raise SSHSecurityError("remote script contains a NUL byte")
        return (*self.config.base_argv(), self.config.target, remote_script)

    @staticmethod
    def _remote_environment(env: Mapping[str, str]) -> str:
        if not env:
            return ""
        assignments = " ".join(f"{key}={shlex.quote(value)}" for key, value in sorted(env.items()))
        return f"env {assignments} "

    def _submit_script(self, spec: JobSpec, job_id: str) -> str:
        remote_dir = self._remote_dir(job_id)
        log_path = remote_dir / "job.log"
        pid_path = remote_dir / "pid"
        exit_path = remote_dir / "exit_code"
        cancelled_path = remote_dir / "cancelled"
        workdir = PurePosixPath(str(spec.workdir).replace("\\", "/"))
        if ".." in workdir.parts or "\x00" in workdir.as_posix():
            raise SSHSecurityError("remote workdir must be traversal-free")
        command = shlex.join(spec.command)
        payload = (
            f"(cd {shlex.quote(workdir.as_posix())} && "
            f"{self._remote_environment(spec.env)}{command}); "
            "rc=$?; "
            f"printf '%s\\n' \"$rc\" > {shlex.quote(exit_path.as_posix())}; "
            'exit "$rc"'
        )
        return (
            f"mkdir -p {shlex.quote(remote_dir.as_posix())} && "
            f"rm -f {shlex.quote(exit_path.as_posix())} "
            f"{shlex.quote(cancelled_path.as_posix())} && "
            "{ "
            f"nohup sh -c {shlex.quote(payload)} "
            f"> {shlex.quote(log_path.as_posix())} 2>&1 < /dev/null & "
            "pid=$!; "
            f"printf '%s\\n' \"$pid\" > {shlex.quote(pid_path.as_posix())}; "
            "printf '%s\\n' \"$pid\"; "
            "}"
        )

    def _submit_argv(self, spec: JobSpec, job_id: str) -> tuple[str, ...]:
        return self._ssh_argv(self._submit_script(spec, job_id))

    def submit(self, spec: JobSpec, *, execute: bool | None = None) -> JobResult:
        job_id = self._job_id(spec)
        argv = self._submit_argv(spec, job_id)
        plan = self._plan(
            job_id=job_id,
            action="submit",
            argv=argv,
            description=f"submit SSH job {job_id} with strict host verification",
            environment_keys=tuple(spec.env),
            metadata={
                "target": self.config.target,
                "remote_dir": self._remote_dir(job_id).as_posix(),
            },
            sensitive_values=self._sensitive_environment_values(spec.env),
        )
        self._remember(job_id, spec=spec, result=plan)
        if not self._should_execute(spec, execute):
            return plan
        self._reject_sensitive_remote_environment(spec)
        outcome = self._run(
            spec,
            argv,
            timeout=spec.resources.timeout_seconds or 60,
        )
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
            message="SSH job submitted" if outcome.return_code == 0 else "SSH submit failed",
            metadata={
                "target": self.config.target,
                "remote_dir": self._remote_dir(job_id).as_posix(),
                "remote_pid": outcome.stdout.strip() or None,
            },
            created_at=plan.created_at,
        )
        self._remember(job_id, result=result)
        return result

    def _status_argv(self, job_id: str) -> tuple[str, ...]:
        remote_dir = self._remote_dir(job_id)
        pid_path = remote_dir / "pid"
        exit_path = remote_dir / "exit_code"
        cancelled_path = remote_dir / "cancelled"
        script = (
            f"if [ -f {shlex.quote(cancelled_path.as_posix())} ]; then "
            "printf 'CANCELLED\\n'; "
            f"elif [ -f {shlex.quote(exit_path.as_posix())} ]; then "
            f"rc=$(cat {shlex.quote(exit_path.as_posix())}); "
            "if [ \"$rc\" = 0 ]; then printf 'SUCCEEDED:0\\n'; "
            "else printf 'FAILED:%s\\n' \"$rc\"; fi; "
            f"elif [ -f {shlex.quote(pid_path.as_posix())} ] && "
            f'kill -0 "$(cat {shlex.quote(pid_path.as_posix())})" 2>/dev/null; '
            "then printf 'RUNNING\\n'; "
            "else printf 'UNKNOWN\\n'; fi"
        )
        return self._ssh_argv(script)

    def status(self, job_id: str, *, execute: bool = False) -> JobResult:
        spec = self._known_spec(job_id)
        argv = self._status_argv(job_id)
        plan = self._plan(
            job_id=job_id,
            action="status",
            argv=argv,
            description=f"inspect SSH job {job_id}",
        )
        if not execute:
            return plan
        outcome = self._run(spec, argv, timeout=60)
        raw = outcome.stdout.strip().splitlines()[-1] if outcome.stdout.strip() else ""
        state_text, _, code_text = raw.partition(":")
        mapping = {
            "RUNNING": JobStatus.RUNNING,
            "SUCCEEDED": JobStatus.SUCCEEDED,
            "FAILED": JobStatus.FAILED,
            "CANCELLED": JobStatus.CANCELLED,
            "UNKNOWN": JobStatus.UNKNOWN,
        }
        status = mapping.get(state_text, JobStatus.UNKNOWN)
        if outcome.return_code != 0:
            status = JobStatus.UNKNOWN
        return_code = int(code_text) if code_text.lstrip("-").isdigit() else None
        previous = self._results.get(job_id)
        result = JobResult(
            job_id=job_id,
            backend=self.name,
            status=status,
            executed=True,
            plan=plan.plan,
            return_code=return_code,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            message=f"SSH job state: {status.value}",
            metadata=previous.metadata if previous else {},
            created_at=previous.created_at if previous else plan.created_at,
        )
        self._remember(job_id, result=result)
        return result

    def cancel(self, job_id: str, *, execute: bool = False) -> JobResult:
        spec = self._known_spec(job_id)
        remote_dir = self._remote_dir(job_id)
        pid_path = remote_dir / "pid"
        cancelled_path = remote_dir / "cancelled"
        script = (
            f"if [ -f {shlex.quote(pid_path.as_posix())} ]; then "
            f"pid=$(cat {shlex.quote(pid_path.as_posix())}); "
            'kill "$pid" 2>/dev/null || true; fi; '
            f"printf 'CANCELLED\\n' > {shlex.quote(cancelled_path.as_posix())}; "
            "printf 'CANCELLED\\n'"
        )
        argv = self._ssh_argv(script)
        plan = self._plan(
            job_id=job_id,
            action="cancel",
            argv=argv,
            description=f"cancel SSH job {job_id}",
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
            message="SSH job cancelled" if outcome.return_code == 0 else "SSH cancel failed",
            metadata={
                "target": self.config.target,
                "remote_dir": remote_dir.as_posix(),
            },
        )
        self._remember(job_id, result=result)
        return result

    def resume(self, job_id: str, *, execute: bool = False) -> JobResult:
        spec = self._known_spec(job_id)
        argv = self._submit_argv(spec, job_id)
        plan = self._plan(
            job_id=job_id,
            action="resume",
            argv=argv,
            description=f"restart SSH job {job_id}",
            environment_keys=tuple(spec.env),
            sensitive_values=self._sensitive_environment_values(spec.env),
        )
        if not execute:
            return plan
        self._reject_sensitive_remote_environment(spec)
        outcome = self._run(
            spec,
            argv,
            timeout=spec.resources.timeout_seconds or 60,
        )
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
            message="SSH job resumed" if outcome.return_code == 0 else "SSH resume failed",
            metadata={
                "target": self.config.target,
                "remote_dir": self._remote_dir(job_id).as_posix(),
                "remote_pid": outcome.stdout.strip() or None,
            },
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
        log_path = self._remote_dir(job_id) / "job.log"
        if tail is None:
            script = f"cat {shlex.quote(log_path.as_posix())}"
        else:
            script = f"tail -n {tail} {shlex.quote(log_path.as_posix())}"
        if follow:
            script = f"tail -f {shlex.quote(log_path.as_posix())}"
        argv = self._ssh_argv(script)
        plan = self._plan(
            job_id=job_id,
            action="logs",
            argv=argv,
            description=f"read SSH job log for {job_id}",
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
            message="SSH log snapshot",
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
        if direction is ArtifactDirection.UPLOAD:
            unsafe = [
                path
                for path in selected
                if Path(path).name == ".env"
                or Path(path).suffix.casefold() in {".key", ".pem", ".p12", ".pfx"}
                or _SECRET_UPLOAD_PATTERN.search(Path(path).name)
            ]
            if unsafe:
                raise SSHSecurityError(
                    "SSH upload denylist rejected sensitive artifact paths: "
                    + ", ".join(sorted(unsafe))
                )
        local_root = Path(local_path).expanduser().resolve()
        remote_workdir = PurePosixPath(str(spec.workdir).replace("\\", "/"))
        commands: list[tuple[str, ...]] = []
        for pattern in selected:
            remote_path = (remote_workdir / pattern).as_posix()
            remote_arg = f"{self.config.target}:{shlex.quote(remote_path)}"
            local_target = local_root / pattern
            if direction is ArtifactDirection.DOWNLOAD:
                argv = (
                    *self.config.scp_base_argv(),
                    "-r",
                    remote_arg,
                    str(local_target),
                )
            else:
                argv = (
                    *self.config.scp_base_argv(),
                    "-r",
                    str(local_target),
                    remote_arg,
                )
            commands.append(argv)
        plan = self._plan(
            job_id=job_id,
            action="artifact-sync",
            argv=commands[0],
            description=f"{direction.value} SSH artifacts for {job_id}",
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
            outcome = self._run(spec, command, timeout=spec.resources.timeout_seconds)
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
                "SSH artifact sync completed" if return_code == 0 else "SSH artifact sync failed"
            ),
            metadata={"paths": list(selected), "direction": direction.value},
        )


SSHComputeBackend = SSHBackend
