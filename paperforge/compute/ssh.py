from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from paperforge.policy import Action

from ._artifacts import (
    artifact_patterns,
    copy_local_artifacts,
    file_record,
    safe_artifact_destination,
    safe_artifact_file,
    safe_artifact_root,
)
from .base import CommandOutcome, ComputeBackend
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


class SSHSecurityError(ValueError):
    pass


_HOST_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:-]{0,252}$")
_USER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
_EXECUTABLE_PATTERN = re.compile(r"^[A-Za-z0-9_./-]+$")
_IMAGE_DIGEST = re.compile(r"^.+@sha256:[0-9a-fA-F]{64}$")
_CONTAINER_USER = re.compile(r"^[1-9][0-9]{0,9}:[1-9][0-9]{0,9}$")
_SECRET_UPLOAD_PATTERN = re.compile(
    r"(?:^|[._-])(?:credential|identity|private|secret|token|key)(?:[._-]|$)",
    re.IGNORECASE,
)
_OUTPUT_MOUNT_ROOT = PurePosixPath("/paperforge-outputs")
_WINDOWS_TRUSTED_CONTROL_SIDS = frozenset(
    {
        "S-1-5-18",
        "S-1-5-32-544",
    }
)
_WINDOWS_WRITE_MASK = (
    0x10000000
    | 0x40000000
    | 0x00000002
    | 0x00000004
    | 0x00000010
    | 0x00000100
    | 0x00010000
    | 0x00040000
    | 0x00080000
)
_WINDOWS_SYNCHRONIZE = 0x00100000


def _validate_windows_acl_payload(
    payload: Mapping[str, Any],
    *,
    label: str,
    private: bool,
) -> None:
    if payload.get("dacl_present") is not True or payload.get("dacl_null") is not False:
        raise SSHSecurityError(f"{label} has an unsafe Windows DACL")
    current = str(payload.get("current", "")).strip()
    owner = str(payload.get("owner", "")).strip()
    trusted = _WINDOWS_TRUSTED_CONTROL_SIDS | {current}
    if not current or owner not in trusted:
        raise SSHSecurityError(f"{label} has an untrusted Windows owner")

    rules = payload.get("rules", [])
    if isinstance(rules, Mapping):
        rules = [rules]
    if not isinstance(rules, list):
        raise SSHSecurityError(f"{label} has an unreadable Windows ACL")
    for item in rules:
        if not isinstance(item, Mapping):
            raise SSHSecurityError(f"{label} has an unreadable Windows ACL")
        if str(item.get("type", "")).casefold() != "allow":
            continue
        if "inheritonly" in str(item.get("propagation", "")).replace(" ", "").casefold():
            continue
        sid = str(item.get("sid", "")).strip()
        try:
            rights = int(item.get("rights", 0)) & 0xFFFFFFFF
        except (TypeError, ValueError) as exc:
            raise SSHSecurityError(f"{label} has an unreadable Windows ACL") from exc
        if sid in trusted:
            continue
        effective = rights & ~_WINDOWS_SYNCHRONIZE
        if private and effective:
            raise SSHSecurityError(
                f"{label} Windows ACL permits access by an untrusted principal"
            )
        if not private and rights & _WINDOWS_WRITE_MASK:
            raise SSHSecurityError(
                f"{label} Windows ACL permits writes by an untrusted principal"
            )


def _validate_windows_acl(path: Path, *, label: str, private: bool) -> None:
    powershell = (
        shutil.which("powershell.exe")
        or shutil.which("pwsh.exe")
        or shutil.which("powershell")
        or shutil.which("pwsh")
    )
    if powershell is None:
        raise SSHSecurityError(f"{label} Windows ACL cannot be verified")
    script = r"""
$ErrorActionPreference = "Stop"
$acl = Get-Acl -LiteralPath $args[0]
$descriptor = [System.Security.AccessControl.RawSecurityDescriptor]::new(
    $acl.GetSecurityDescriptorBinaryForm(),
    0
)
$daclPresent = (
    $descriptor.ControlFlags -band
    [System.Security.AccessControl.ControlFlags]::DiscretionaryAclPresent
) -ne 0
$current = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$owner = ([System.Security.Principal.NTAccount]$acl.Owner).Translate(
    [System.Security.Principal.SecurityIdentifier]
).Value
$rules = @($acl.Access | ForEach-Object {
    [PSCustomObject]@{
        sid = $_.IdentityReference.Translate(
            [System.Security.Principal.SecurityIdentifier]
        ).Value
        rights = [Int64]$_.FileSystemRights
        type = $_.AccessControlType.ToString()
        propagation = $_.PropagationFlags.ToString()
    }
})
[PSCustomObject]@{
    dacl_present = $daclPresent
    dacl_null = $null -eq $descriptor.DiscretionaryAcl
    current = $current
    owner = $owner
    rules = $rules
} | ConvertTo-Json -Compress -Depth 4
"""
    completed = subprocess.run(
        [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise SSHSecurityError(f"{label} Windows ACL cannot be verified")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise SSHSecurityError(f"{label} Windows ACL cannot be verified") from exc
    if not isinstance(payload, Mapping):
        raise SSHSecurityError(f"{label} Windows ACL cannot be verified")
    _validate_windows_acl_payload(payload, label=label, private=private)


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
    if os.name == "nt":
        _validate_windows_acl(resolved, label=label, private=private)
        return resolved
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
    remote_container_runtime: str = "podman"
    remote_container_runtime_sha256: str | None = None
    remote_container_image: str | None = None
    remote_container_user: str = "host"

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
        if not _EXECUTABLE_PATTERN.fullmatch(
            self.remote_container_runtime
        ):
            raise SSHSecurityError(
                "remote_container_runtime contains unsafe characters"
            )
        if (
            self.remote_container_runtime_sha256 is not None
            and not re.fullmatch(
                r"[0-9a-fA-F]{64}",
                self.remote_container_runtime_sha256,
            )
        ):
            raise SSHSecurityError(
                "remote_container_runtime_sha256 must be a sha256 digest"
            )
        if (
            self.remote_container_image is not None
            and not _IMAGE_DIGEST.fullmatch(
                self.remote_container_image
            )
        ):
            raise SSHSecurityError(
                "remote_container_image must be pinned by sha256 digest"
            )
        if (
            self.remote_container_user != "host"
            and not _CONTAINER_USER.fullmatch(self.remote_container_user)
        ):
            raise SSHSecurityError(
                "remote_container_user must be 'host' or a non-root uid:gid pair"
            )
        root = PurePosixPath(self.remote_root)
        if (
            root.is_absolute()
            or ".." in root.parts
            or "\x00" in self.remote_root
            or not re.fullmatch(r"[A-Za-z0-9._/-]+", self.remote_root)
        ):
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
            "remote_container_runtime": self.remote_container_runtime,
            "remote_container_runtime_sha256": (
                self.remote_container_runtime_sha256
            ),
            "remote_container_image": self.remote_container_image,
            "remote_container_user": self.remote_container_user,
        }


class SSHBackend(ComputeBackend):
    name = "ssh"
    policy_action = Action.REMOTE_EXECUTE

    def __init__(self, config: SSHConfig, **kwargs: Any) -> None:
        self.config = config
        super().__init__(**kwargs)

    def _remote_dir(self, job_id: str) -> PurePosixPath:
        return PurePosixPath(self.config.remote_root) / job_id

    def _remote_workdir(self, spec: JobSpec, job_id: str) -> PurePosixPath:
        if spec.metadata.get("remote_source_sha256"):
            return self._remote_dir(job_id) / "workspace"
        return PurePosixPath(str(spec.workdir).replace("\\", "/"))

    def _remote_attempt_dir(self, job_id: str, attempt: int) -> PurePosixPath:
        return self._remote_dir(job_id) / "attempts" / str(attempt)

    def _remote_artifact_root(
        self,
        job_id: str,
        attempt: int = 1,
    ) -> PurePosixPath:
        return self._remote_attempt_dir(job_id, attempt) / "artifacts"

    @staticmethod
    def _remote_container_name(job_id: str, attempt: int = 1) -> str:
        digest = hashlib.sha256(f"{job_id}:{attempt}".encode()).hexdigest()[:20]
        return f"paperforge-ssh-{digest}"

    def _binding_digest(self, spec: JobSpec) -> str:
        payload = {
            "job_fingerprint": spec.fingerprint,
            "host": self.config.host,
            "port": self.config.port,
            "runtime_sha256": self.config.remote_container_runtime_sha256,
            "image": self.config.remote_container_image,
            "container_user": self.config.remote_container_user,
        }
        encoded = json.dumps(
            payload,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    def _attempt_nonce(
        self,
        spec: JobSpec,
        job_id: str,
        attempt: int,
    ) -> str:
        return hashlib.sha256(
            f"{job_id}:{attempt}:{self._binding_digest(spec)}".encode()
        ).hexdigest()

    def _ssh_argv(self, remote_script: str) -> tuple[str, ...]:
        if "\x00" in remote_script:
            raise SSHSecurityError("remote script contains a NUL byte")
        return (*self.config.base_argv(), self.config.target, remote_script)

    def _container_user_argument(self) -> str:
        if self.config.remote_container_user == "host":
            return '"$(id -u):$(id -g)"'
        return shlex.quote(self.config.remote_container_user)

    @staticmethod
    def _remote_regular_file_check(path: PurePosixPath) -> str:
        current = PurePosixPath()
        checks: list[str] = []
        for part in path.parts:
            current /= part
            rendered = shlex.quote(current.as_posix())
            checks.append(f"[ ! -L {rendered} ]")
        checks.append(f"[ -f {shlex.quote(path.as_posix())} ]")
        return " && ".join(checks)

    @staticmethod
    def _remote_parent_prepare(path: PurePosixPath) -> str:
        current = PurePosixPath()
        commands: list[str] = []
        for part in path.parent.parts:
            current /= part
            rendered = shlex.quote(current.as_posix())
            commands.append(
                f"if [ -e {rendered} ]; then "
                f"[ -d {rendered} ] && [ ! -L {rendered} ]; "
                f"else mkdir {rendered}; fi"
            )
        target = shlex.quote(path.as_posix())
        commands.append(f"[ ! -L {target} ]")
        return " && ".join(commands)

    @staticmethod
    def _remote_environment(env: Mapping[str, str]) -> str:
        if not env:
            return ""
        assignments = " ".join(f"{key}={shlex.quote(value)}" for key, value in sorted(env.items()))
        return f"env {assignments} "

    def _submit_script(
        self,
        spec: JobSpec,
        job_id: str,
        *,
        attempt: int,
        nonce: str,
        binding_digest: str,
    ) -> str:
        remote_dir = self._remote_dir(job_id)
        attempt_dir = self._remote_attempt_dir(job_id, attempt)
        log_path = attempt_dir / "job.log"
        pid_path = attempt_dir / "pid"
        start_path = attempt_dir / "pid_start"
        identity_path = attempt_dir / "identity"
        exit_path = attempt_dir / "exit_code"
        cancelled_path = attempt_dir / "cancelled"
        timed_out_path = attempt_dir / "timed_out"
        cid_path = attempt_dir / "container_id"
        workdir = self._remote_workdir(spec, job_id)
        if ".." in workdir.parts or "\x00" in workdir.as_posix():
            raise SSHSecurityError("remote workdir must be traversal-free")
        if spec.metadata.get("remote_source_sha256"):
            command = self._container_command(
                spec,
                job_id,
                attempt=attempt,
                cid_path=cid_path,
            )
            runtime = shlex.quote(self.config.remote_container_runtime)
            container_name = shlex.quote(
                self._remote_container_name(job_id, attempt)
            )
            invocation = command
            cleanup = (
                f"cid=$(cat {shlex.quote(cid_path.as_posix())} 2>/dev/null || true); "
                f"if [ -n \"$cid\" ] && "
                f"[ \"$({runtime} inspect --format '{{{{.Id}}}}' {container_name} 2>/dev/null)\" = \"$cid\" ]; then "
                f"{runtime} rm -f {container_name} >/dev/null 2>&1 || true; fi; "
            )
        else:
            command = shlex.join(spec.command)
            invocation = (
                f"cd {shlex.quote(workdir.as_posix())} && "
                f"{self._remote_environment(spec.env)}{command}"
            )
            cleanup = ""
        payload = (
            f"({invocation}); "
            "rc=$?; "
            f"printf '%s\\n' \"$rc\" > {shlex.quote(exit_path.as_posix())}; "
            f"{cleanup}"
            'exit "$rc"'
        )
        artifact_root = self._remote_artifact_root(job_id, attempt)
        output_setup = " && ".join(
            (
                f"mkdir -p {shlex.quote(str(artifact_root / output).rsplit('/', 1)[0])} "
                f"&& : > {shlex.quote((artifact_root / output).as_posix())} "
                f"&& chmod u+rw,go-rwx {shlex.quote((artifact_root / output).as_posix())}"
            )
            for output in spec.outputs
        )
        expected_identity = "\n".join(
            (
                nonce,
                binding_digest,
                str(attempt),
            )
        )
        timeout_script = ""
        if spec.resources.timeout_seconds is not None:
            identity_check = (
                f"[ -f {shlex.quote(identity_path.as_posix())} ] && "
                f"[ \"$(sed -n '1p' {shlex.quote(identity_path.as_posix())})\" = {shlex.quote(nonce)} ] && "
                f"[ \"$(sed -n '2p' {shlex.quote(identity_path.as_posix())})\" = {shlex.quote(binding_digest)} ] && "
                f"[ \"$(sed -n '3p' {shlex.quote(identity_path.as_posix())})\" = {shlex.quote(str(attempt))} ] && "
                f"pid=$(cat {shlex.quote(pid_path.as_posix())}) && "
                f"saved_start=$(cat {shlex.quote(start_path.as_posix())}) && "
                "[ -r \"/proc/$pid/stat\" ] && "
                "[ \"$(awk '{print $22}' \"/proc/$pid/stat\")\" = \"$saved_start\" ]"
            )
            stop_container = ""
            if spec.metadata.get("remote_source_sha256"):
                stop_container = (
                    f"cid=$(cat {shlex.quote(cid_path.as_posix())} 2>/dev/null || true); "
                    f"if [ -n \"$cid\" ] && [ \"$({runtime} inspect --format '{{{{.Id}}}}' {container_name} 2>/dev/null)\" = \"$cid\" ]; then "
                    f"{runtime} rm -f {container_name} >/dev/null 2>&1 || true; fi; "
                )
            watcher = (
                f"sleep {int(spec.resources.timeout_seconds)}; "
                f"if [ ! -f {shlex.quote(exit_path.as_posix())} ] && "
                f"[ ! -f {shlex.quote(cancelled_path.as_posix())} ] && "
                f"{identity_check}; then "
                f"printf 'TIMED_OUT\\n' > {shlex.quote(timed_out_path.as_posix())}; "
                f"{stop_container}kill \"$pid\" 2>/dev/null || true; fi"
            )
            timeout_script = (
                f"nohup sh -c {shlex.quote(watcher)} "
                "> /dev/null 2>&1 < /dev/null & "
            )
        source_check = ""
        if spec.metadata.get("remote_source_sha256"):
            source_check = (
                f"[ \"$(cat {shlex.quote((remote_dir / 'source.sha256').as_posix())})\" = "
                f"{shlex.quote(str(spec.metadata['remote_source_sha256']))} ] && "
            )
        parent_guard = self._remote_parent_prepare(attempt_dir / ".guard")
        prepare = (
            f"{parent_guard} && "
            f"rm -rf {shlex.quote(attempt_dir.as_posix())} && "
            f"mkdir -p {shlex.quote(attempt_dir.as_posix())} "
            f"{shlex.quote(artifact_root.as_posix())}"
        )
        if self.config.remote_container_user == "host":
            prepare = f'[ "$(id -u)" -ne 0 ] && {prepare}'
        if output_setup:
            prepare += f" && {output_setup}"
        return (
            f"{prepare} && {source_check}"
            "{ "
            f"nohup sh -c {shlex.quote(payload)} "
            f"> {shlex.quote(log_path.as_posix())} 2>&1 < /dev/null & "
            "pid=$!; "
            f"printf '%s\\n' \"$pid\" > {shlex.quote(pid_path.as_posix())}; "
            "start=$(awk '{print $22}' \"/proc/$pid/stat\"); "
            f"printf '%s\\n' \"$start\" > {shlex.quote(start_path.as_posix())}; "
            f"printf '%s\\n' {shlex.quote(expected_identity)} > {shlex.quote(identity_path.as_posix())}; "
            f"{timeout_script}"
            "i=0; while [ $i -lt 50 ] && "
            f"[ ! -s {shlex.quote(cid_path.as_posix())} ]; do sleep 0.1; i=$((i+1)); done; "
            f"cid=$(cat {shlex.quote(cid_path.as_posix())} 2>/dev/null || true); "
            "printf '%s|%s|%s\\n' \"$pid\" \"$start\" \"$cid\"; "
            "}"
        )

    def _failed_submit_cleanup_script(
        self,
        spec: JobSpec,
        job_id: str,
        *,
        attempt: int,
        nonce: str,
        binding_digest: str,
    ) -> str:
        attempt_dir = self._remote_attempt_dir(job_id, attempt)
        identity_path = attempt_dir / "identity"
        pid_path = attempt_dir / "pid"
        start_path = attempt_dir / "pid_start"
        cid_path = attempt_dir / "container_id"
        identity_check = (
            f"[ \"$(sed -n '1p' {shlex.quote(identity_path.as_posix())} 2>/dev/null)\" = {shlex.quote(nonce)} ] && "
            f"[ \"$(sed -n '2p' {shlex.quote(identity_path.as_posix())} 2>/dev/null)\" = {shlex.quote(binding_digest)} ] && "
            f"[ \"$(sed -n '3p' {shlex.quote(identity_path.as_posix())} 2>/dev/null)\" = {shlex.quote(str(attempt))} ] && "
            f"pid=$(cat {shlex.quote(pid_path.as_posix())} 2>/dev/null) && "
            f"start=$(cat {shlex.quote(start_path.as_posix())} 2>/dev/null) && "
            "[ -n \"$pid\" ] && [ -n \"$start\" ] && "
            "[ -r \"/proc/$pid/stat\" ] && "
            "[ \"$(awk '{print $22}' \"/proc/$pid/stat\")\" = \"$start\" ]"
        )
        container_cleanup = ""
        if spec.metadata.get("remote_source_sha256"):
            runtime = shlex.quote(self.config.remote_container_runtime)
            name = shlex.quote(self._remote_container_name(job_id, attempt))
            container_cleanup = (
                f"cid=$(cat {shlex.quote(cid_path.as_posix())} 2>/dev/null || true); "
                f"if [ -n \"$cid\" ] && "
                f"[ \"$({runtime} inspect --format '{{{{.Id}}}}' {name} 2>/dev/null)\" = \"$cid\" ]; then "
                f"{runtime} rm -f {name} >/dev/null 2>&1 || true; fi; "
            )
        return (
            f"if {identity_check}; then {container_cleanup}"
            "kill \"$pid\" 2>/dev/null || true; printf 'CLEANED\\n'; "
            "else printf 'UNCHANGED\\n'; fi"
        )

    def _cleanup_failed_launch(
        self,
        spec: JobSpec,
        job_id: str,
        *,
        attempt: int,
        nonce: str,
        binding_digest: str,
    ) -> CommandOutcome:
        try:
            return self._run(
                spec,
                self._ssh_argv(
                    self._failed_submit_cleanup_script(
                        spec,
                        job_id,
                        attempt=attempt,
                        nonce=nonce,
                        binding_digest=binding_digest,
                    )
                ),
                timeout=60,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return CommandOutcome(
                1,
                "",
                f"SSH failed-launch cleanup transport error ({type(exc).__name__})\n",
            )

    @staticmethod
    def _cleanup_outcome_status(outcome: CommandOutcome) -> str:
        if outcome.return_code == 0 and "CLEANED" in outcome.stdout:
            return "CLEANED"
        if outcome.return_code == 0 and "UNCHANGED" in outcome.stdout:
            return "UNCHANGED"
        return "FAILED"

    def _container_command(
        self,
        spec: JobSpec,
        job_id: str,
        *,
        attempt: int,
        cid_path: PurePosixPath,
    ) -> str:
        image = self.config.remote_container_image
        runtime_sha256 = self.config.remote_container_runtime_sha256
        if image is None or runtime_sha256 is None:
            raise SSHSecurityError(
                "executable SSH jobs require a pinned remote container "
                "image and runtime sha256"
            )
        if any(
            character in str(path)
            for path in spec.outputs
            for character in "*?[]"
        ):
            raise SSHSecurityError(
                "executable SSH outputs must be explicit files"
            )
        remote_dir = self._remote_dir(job_id)
        workspace = f"$HOME/{(remote_dir / 'workspace').as_posix()}"
        artifacts = f"$HOME/{self._remote_artifact_root(job_id, attempt).as_posix()}"
        parts = [
            shlex.quote(self.config.remote_container_runtime),
            "run",
            "--name",
            shlex.quote(self._remote_container_name(job_id, attempt)),
            "--cidfile",
            f'"$HOME/{cid_path.as_posix()}"',
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "512",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=256m",
            "--user",
            self._container_user_argument(),
            "--workdir",
            "/workspace",
            "--volume",
            f'"{workspace}:/workspace:ro"',
        ]
        if spec.outputs:
            parts.extend(
                [
                    "--tmpfs",
                    f"{_OUTPUT_MOUNT_ROOT}:rw,noexec,nosuid,nodev,size=64m",
                ]
            )
        for index, output in enumerate(spec.outputs):
            host_output = (
                f"{artifacts}/{PurePosixPath(str(output)).as_posix()}"
            )
            parts.extend(
                [
                    "--volume",
                    f'"{host_output}:{(_OUTPUT_MOUNT_ROOT / str(index)).as_posix()}:rw"',
                ]
            )
        for key, value in sorted(spec.env.items()):
            parts.extend(["--env", shlex.quote(f"{key}={value}")])
        parts.extend(
            [
                "--cpus",
                str(spec.resources.cpus),
            ]
        )
        if spec.resources.memory_mb is not None:
            parts.extend(
                ["--memory", f"{spec.resources.memory_mb}m"]
            )
        parts.append(shlex.quote(image))
        parts.extend(shlex.quote(part) for part in spec.command)
        return " ".join(parts)

    def _submit_argv(
        self,
        spec: JobSpec,
        job_id: str,
        *,
        attempt: int = 1,
    ) -> tuple[str, ...]:
        return self._ssh_argv(
            self._submit_script(
                spec,
                job_id,
                attempt=attempt,
                nonce=self._attempt_nonce(spec, job_id, attempt),
                binding_digest=self._binding_digest(spec),
            )
        )

    def stage_source(
        self,
        spec: JobSpec,
        source_snapshot: str | Path,
        *,
        execute: bool = False,
    ) -> JobResult:
        job_id = self._job_id(spec)
        expected_sha256 = str(spec.metadata.get("remote_source_sha256") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise SSHSecurityError(
                "SSH source staging requires a bound source sha256"
            )
        try:
            bundle = create_verified_source_bundle(
                source_snapshot,
                canonical_worktree=spec.workdir,
                expected_source_sha256=expected_sha256,
                staging_dir=self._job_state_dir(job_id),
            )
        except SourceBundleError as exc:
            raise SSHSecurityError(str(exc)) from exc
        remote_dir = self._remote_dir(job_id)
        remote_workdir = self._remote_workdir(spec, job_id)
        remote_archive = remote_dir / "source.tar"
        prepare = self._ssh_argv(
            self._remote_parent_prepare(remote_archive)
        )
        upload = (
            *self.config.scp_base_argv(),
            str(bundle.path),
            (
                f"{self.config.target}:"
                f"{shlex.quote(remote_archive.as_posix())}"
            ),
        )
        runtime_check = (
            "runtime_path=$(command -v "
            f"{shlex.quote(self.config.remote_container_runtime)}) && "
            '[ -n "$runtime_path" ] && '
            '[ "$(sha256sum "$runtime_path" | cut -d " " -f 1)" = '
            f"{shlex.quote(str(self.config.remote_container_runtime_sha256 or ''))} ]"
        )
        extract_script = (
            f"printf '%s  %s\\n' "
            f"{shlex.quote(bundle.archive_sha256)} "
            f"{shlex.quote(remote_archive.as_posix())} | sha256sum -c - && "
            f"rm -rf {shlex.quote(remote_workdir.as_posix())} && "
            f"mkdir -p {shlex.quote(remote_workdir.as_posix())} && "
            f"tar -xf {shlex.quote(remote_archive.as_posix())} "
            f"-C {shlex.quote(remote_workdir.as_posix())} && "
            f"rm -f {shlex.quote(remote_archive.as_posix())} && "
            f"printf '%s\\n' {shlex.quote(expected_sha256)} > "
            f"{shlex.quote((remote_dir / 'source.sha256').as_posix())}"
        )
        extract_script += (
            f" && chmod -R u+rwX,go+rX,go-w "
            f"{shlex.quote(remote_workdir.as_posix())}"
        )
        output_setup_parts: list[str] = []
        for index, output in enumerate(spec.outputs):
            source_output = remote_workdir / output
            output_setup_parts.append(
                f"mkdir -p {shlex.quote(source_output.parent.as_posix())} && "
                f"rm -f {shlex.quote(source_output.as_posix())} && "
                f"ln -s {shlex.quote((_OUTPUT_MOUNT_ROOT / str(index)).as_posix())} "
                f"{shlex.quote(source_output.as_posix())}"
            )
        if output_setup_parts:
            extract_script += " && " + " && ".join(output_setup_parts)
        extract_script += f" && {runtime_check}"
        verify_and_extract = self._ssh_argv(extract_script)
        plan = self._plan(
            job_id=job_id,
            action="source-stage",
            argv=upload,
            description=f"stage immutable source for SSH job {job_id}",
            metadata={
                "commands": [
                    list(prepare),
                    list(upload),
                    list(verify_and_extract),
                ],
                "remote_workdir": remote_workdir.as_posix(),
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
        try:
            for command in (prepare, upload, verify_and_extract):
                outcome = self._run(
                    spec,
                    command,
                    timeout=spec.resources.timeout_seconds or 300,
                )
                stdout.append(outcome.stdout)
                stderr.append(outcome.stderr)
                if outcome.return_code != 0:
                    return JobResult(
                        job_id=job_id,
                        backend=self.name,
                        status=JobStatus.FAILED,
                        executed=True,
                        plan=plan.plan,
                        return_code=outcome.return_code,
                        stdout="".join(stdout),
                        stderr="".join(stderr),
                        message="SSH source staging failed",
                    )
        finally:
            bundle.path.unlink(missing_ok=True)
        return JobResult(
            job_id=job_id,
            backend=self.name,
            status=JobStatus.SUCCEEDED,
            executed=True,
            plan=plan.plan,
            return_code=0,
            stdout="".join(stdout),
            stderr="".join(stderr),
            message="SSH source staging completed",
            metadata={
                "remote_workdir": remote_workdir.as_posix(),
                "source_sha256": expected_sha256,
                "archive_sha256": bundle.archive_sha256,
            },
        )

    def submit(self, spec: JobSpec, *, execute: bool | None = None) -> JobResult:
        job_id = self._job_id(spec)
        attempt = 1
        argv = self._submit_argv(spec, job_id, attempt=attempt)
        nonce = self._attempt_nonce(spec, job_id, attempt)
        binding_digest = self._binding_digest(spec)
        plan = self._plan(
            job_id=job_id,
            action="submit",
            argv=argv,
            description=f"submit SSH job {job_id} with strict host verification",
            environment_keys=tuple(spec.env),
            metadata={
                "target": self.config.target,
                "remote_dir": self._remote_dir(job_id).as_posix(),
                "attempt": attempt,
                "identity_nonce": nonce,
                "binding_digest": binding_digest,
            },
            sensitive_values=self._sensitive_environment_values(spec.env),
        )
        self._remember(job_id, spec=spec, result=plan)
        if not self._should_execute(spec, execute):
            return plan
        self._reject_sensitive_remote_environment(spec)
        self._persist_submission_intent(spec, plan)
        transport_error: str | None = None
        try:
            outcome = self._run(
                spec,
                argv,
                timeout=60,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            transport_error = type(exc).__name__
            outcome = CommandOutcome(
                1,
                "",
                f"SSH submit transport error ({transport_error})\n",
            )
        status = JobStatus.SUBMITTED if outcome.return_code == 0 else JobStatus.FAILED
        identity_line = outcome.stdout.strip().splitlines()[-1] if outcome.stdout.strip() else ""
        remote_pid, _, remainder = identity_line.partition("|")
        remote_start, _, remote_container_id = remainder.partition("|")
        if status is JobStatus.SUBMITTED and (
            not remote_pid.isdigit()
            or not remote_start.isdigit()
            or (
                spec.metadata.get("remote_source_sha256")
                and not re.fullmatch(
                    r"[0-9a-fA-F]{64}",
                    remote_container_id,
                )
            )
        ):
            status = JobStatus.FAILED
        cleanup_attempted = status is JobStatus.FAILED
        cleanup_status: str | None = None
        if cleanup_attempted:
            cleanup = self._cleanup_failed_launch(
                spec,
                job_id,
                attempt=attempt,
                nonce=nonce,
                binding_digest=binding_digest,
            )
            cleanup_status = self._cleanup_outcome_status(cleanup)
            outcome = CommandOutcome(
                outcome.return_code,
                outcome.stdout + cleanup.stdout,
                outcome.stderr + cleanup.stderr,
            )
        cleanup_pending = cleanup_attempted and cleanup_status != "CLEANED"
        if cleanup_pending:
            status = JobStatus.UNKNOWN
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
                "SSH job submitted"
                if status is JobStatus.SUBMITTED
                else "SSH submit failed"
            ),
            metadata={
                "target": self.config.target,
                "remote_dir": self._remote_dir(job_id).as_posix(),
                "remote_pid": remote_pid or None,
                "remote_start_time": remote_start or None,
                "remote_container_id": remote_container_id or None,
                "remote_container_name": self._remote_container_name(
                    job_id,
                    attempt,
                ),
                "attempt": attempt,
                "identity_nonce": nonce,
                "binding_digest": binding_digest,
                "deadline_seconds": spec.resources.timeout_seconds,
                "submission_intent": cleanup_pending,
                "cleanup_attempted": cleanup_attempted,
                "cleanup_status": cleanup_status,
                "cleanup_pending": cleanup_pending,
                "transport_error": transport_error,
            },
            created_at=plan.created_at,
        )
        try:
            self._remember(job_id, result=result)
        except Exception:
            if status is JobStatus.SUBMITTED:
                self._run(
                    spec,
                    self._ssh_argv(
                        self._cancel_script(spec, job_id, result.metadata)
                    ),
                    timeout=60,
                )
            raise
        return result

    def _identity_metadata(
        self,
        spec: JobSpec,
        job_id: str,
        result: JobResult,
    ) -> dict[str, Any] | None:
        metadata = dict(result.metadata)
        attempt = metadata.get("attempt")
        pid = str(metadata.get("remote_pid") or "")
        start = str(metadata.get("remote_start_time") or "")
        nonce = str(metadata.get("identity_nonce") or "")
        binding_digest = str(metadata.get("binding_digest") or "")
        if (
            isinstance(attempt, bool)
            or not isinstance(attempt, int)
            or attempt < 1
            or not pid.isdigit()
            or not start.isdigit()
            or nonce != self._attempt_nonce(spec, job_id, attempt)
            or binding_digest != self._binding_digest(spec)
            or metadata.get("remote_container_name")
            != self._remote_container_name(job_id, attempt)
        ):
            return None
        container_id = str(metadata.get("remote_container_id") or "")
        if spec.metadata.get("remote_source_sha256") and not re.fullmatch(
            r"[0-9a-fA-F]{64}",
            container_id,
        ):
            return None
        return metadata

    def _status_argv(
        self,
        spec: JobSpec,
        job_id: str,
        metadata: Mapping[str, Any],
    ) -> tuple[str, ...]:
        attempt = int(metadata["attempt"])
        attempt_dir = self._remote_attempt_dir(job_id, attempt)
        pid_path = attempt_dir / "pid"
        start_path = attempt_dir / "pid_start"
        identity_path = attempt_dir / "identity"
        cid_path = attempt_dir / "container_id"
        exit_path = attempt_dir / "exit_code"
        cancelled_path = attempt_dir / "cancelled"
        timed_out_path = attempt_dir / "timed_out"
        pid = str(metadata["remote_pid"])
        start = str(metadata["remote_start_time"])
        nonce = str(metadata["identity_nonce"])
        binding_digest = str(metadata["binding_digest"])
        expected_cid = str(metadata.get("remote_container_id") or "")
        identity_check = (
            f"[ -f {shlex.quote(identity_path.as_posix())} ] && "
            f"[ \"$(sed -n '1p' {shlex.quote(identity_path.as_posix())})\" = {shlex.quote(nonce)} ] && "
            f"[ \"$(sed -n '2p' {shlex.quote(identity_path.as_posix())})\" = {shlex.quote(binding_digest)} ] && "
            f"[ \"$(sed -n '3p' {shlex.quote(identity_path.as_posix())})\" = {shlex.quote(str(attempt))} ] && "
            f"[ \"$(cat {shlex.quote(pid_path.as_posix())})\" = {shlex.quote(pid)} ] && "
            f"[ \"$(cat {shlex.quote(start_path.as_posix())})\" = {shlex.quote(start)} ]"
        )
        running_identity = (
            f"[ -r /proc/{pid}/stat ] && "
            f"[ \"$(awk '{{print $22}}' /proc/{pid}/stat)\" = {shlex.quote(start)} ]"
        )
        container_identity = ""
        if spec.metadata.get("remote_source_sha256"):
            runtime = shlex.quote(self.config.remote_container_runtime)
            name = shlex.quote(str(metadata["remote_container_name"]))
            container_identity = (
                f" && [ \"$({runtime} inspect --format '{{{{.Id}}}}' {name} 2>/dev/null)\" = {shlex.quote(expected_cid)} ]"
            )
            identity_check += (
                f" && [ \"$(cat {shlex.quote(cid_path.as_posix())})\" = {shlex.quote(expected_cid)} ]"
            )
        script = (
            f"if ! {{ {identity_check}; }}; then printf 'UNKNOWN||\\n'; "
            f"elif [ -f {shlex.quote(timed_out_path.as_posix())} ]; then "
            f"printf 'TIMED_OUT|124|%s\\n' \"$(cat {shlex.quote(cid_path.as_posix())} 2>/dev/null || true)\"; "
            f"elif [ -f {shlex.quote(cancelled_path.as_posix())} ]; then "
            f"printf 'CANCELLED||%s\\n' \"$(cat {shlex.quote(cid_path.as_posix())} 2>/dev/null || true)\"; "
            f"elif [ -f {shlex.quote(exit_path.as_posix())} ]; then "
            f"rc=$(cat {shlex.quote(exit_path.as_posix())}); "
            f"cid=$(cat {shlex.quote(cid_path.as_posix())} 2>/dev/null || true); "
            "if [ \"$rc\" = 0 ]; then printf 'SUCCEEDED|0|%s\\n' \"$cid\"; "
            "else printf 'FAILED|%s|%s\\n' \"$rc\" \"$cid\"; fi; "
            f"elif {running_identity}{container_identity}; then "
            f"printf 'RUNNING||%s\\n' \"$(cat {shlex.quote(cid_path.as_posix())} 2>/dev/null || true)\"; "
            "else printf 'UNKNOWN||\\n'; fi"
        )
        return self._ssh_argv(script)

    def _reconcile_pending_launch(
        self,
        spec: JobSpec,
        job_id: str,
        previous: JobResult,
        *,
        execute: bool,
    ) -> JobResult:
        metadata = dict(previous.metadata)
        raw_attempt = metadata.get("attempt")
        if (
            isinstance(raw_attempt, bool)
            or not isinstance(raw_attempt, int)
            or raw_attempt < 1
        ):
            raise SSHSecurityError("pending SSH launch has an invalid attempt")
        nonce = self._attempt_nonce(spec, job_id, raw_attempt)
        binding_digest = self._binding_digest(spec)
        argv = self._ssh_argv(
            self._failed_submit_cleanup_script(
                spec,
                job_id,
                attempt=raw_attempt,
                nonce=nonce,
                binding_digest=binding_digest,
            )
        )
        plan = self._plan(
            job_id=job_id,
            action="reconcile-pending-launch",
            argv=argv,
            description=f"reconcile unresolved SSH launch {job_id}",
        )
        if not execute:
            return plan
        cleanup = self._cleanup_failed_launch(
            spec,
            job_id,
            attempt=raw_attempt,
            nonce=nonce,
            binding_digest=binding_digest,
        )
        cleanup_status = self._cleanup_outcome_status(cleanup)
        resolved = cleanup_status == "CLEANED"
        metadata.update(
            {
                "submission_intent": not resolved,
                "cleanup_attempted": True,
                "cleanup_status": cleanup_status,
                "cleanup_pending": not resolved,
            }
        )
        result = JobResult(
            job_id=job_id,
            backend=self.name,
            status=JobStatus.FAILED if resolved else JobStatus.UNKNOWN,
            executed=True,
            plan=plan.plan,
            return_code=cleanup.return_code,
            stdout=cleanup.stdout,
            stderr=cleanup.stderr,
            message=(
                "unresolved SSH launch was terminated"
                if resolved
                else "SSH launch cleanup remains unresolved"
            ),
            metadata=metadata,
            created_at=previous.created_at,
        )
        self._remember(job_id, result=result)
        return result

    def status(self, job_id: str, *, execute: bool = False) -> JobResult:
        spec = self._known_spec(job_id)
        previous = self._known_result(job_id)
        if previous.metadata.get("cleanup_pending") is True:
            return self._reconcile_pending_launch(
                spec,
                job_id,
                previous,
                execute=execute,
            )
        metadata = self._identity_metadata(spec, job_id, previous)
        if metadata is None:
            return JobResult(
                job_id=job_id,
                backend=self.name,
                status=JobStatus.UNKNOWN,
                executed=execute,
                plan=previous.plan,
                message="SSH job identity metadata is invalid",
                metadata=previous.metadata,
                created_at=previous.created_at,
            )
        argv = self._status_argv(spec, job_id, metadata)
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
        state_text, _, remainder = raw.partition("|")
        code_text, _, remote_cid = remainder.partition("|")
        mapping = {
            "RUNNING": JobStatus.RUNNING,
            "SUCCEEDED": JobStatus.SUCCEEDED,
            "FAILED": JobStatus.FAILED,
            "CANCELLED": JobStatus.CANCELLED,
            "TIMED_OUT": JobStatus.FAILED,
            "UNKNOWN": JobStatus.UNKNOWN,
        }
        status = mapping.get(state_text, JobStatus.UNKNOWN)
        if outcome.return_code != 0:
            status = JobStatus.UNKNOWN
        return_code = int(code_text) if code_text.lstrip("-").isdigit() else None
        if remote_cid:
            metadata["remote_container_id"] = remote_cid
        if state_text == "TIMED_OUT":
            metadata["timed_out"] = True
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
            metadata=metadata,
            created_at=previous.created_at,
        )
        self._remember(job_id, result=result)
        return result

    def _cancel_script(
        self,
        spec: JobSpec,
        job_id: str,
        metadata: Mapping[str, Any],
    ) -> str:
        attempt = int(metadata["attempt"])
        attempt_dir = self._remote_attempt_dir(job_id, attempt)
        pid_path = attempt_dir / "pid"
        start_path = attempt_dir / "pid_start"
        identity_path = attempt_dir / "identity"
        cid_path = attempt_dir / "container_id"
        cancelled_path = attempt_dir / "cancelled"
        pid = str(metadata["remote_pid"])
        start = str(metadata["remote_start_time"])
        nonce = str(metadata["identity_nonce"])
        binding_digest = str(metadata["binding_digest"])
        identity_check = (
            f"[ \"$(sed -n '1p' {shlex.quote(identity_path.as_posix())} 2>/dev/null)\" = {shlex.quote(nonce)} ] && "
            f"[ \"$(sed -n '2p' {shlex.quote(identity_path.as_posix())} 2>/dev/null)\" = {shlex.quote(binding_digest)} ] && "
            f"[ \"$(sed -n '3p' {shlex.quote(identity_path.as_posix())} 2>/dev/null)\" = {shlex.quote(str(attempt))} ] && "
            f"[ \"$(cat {shlex.quote(pid_path.as_posix())} 2>/dev/null)\" = {shlex.quote(pid)} ] && "
            f"[ \"$(cat {shlex.quote(start_path.as_posix())} 2>/dev/null)\" = {shlex.quote(start)} ] && "
            f"[ -r /proc/{pid}/stat ] && "
            f"[ \"$(awk '{{print $22}}' /proc/{pid}/stat)\" = {shlex.quote(start)} ]"
        )
        cancel_body = (
            f"kill {shlex.quote(pid)} 2>/dev/null || true; "
            f"printf 'CANCELLED\\n' > {shlex.quote(cancelled_path.as_posix())}; "
            "printf 'CANCELLED\\n'"
        )
        if spec.metadata.get("remote_source_sha256"):
            runtime = shlex.quote(self.config.remote_container_runtime)
            name = shlex.quote(str(metadata["remote_container_name"]))
            cid = shlex.quote(str(metadata["remote_container_id"]))
            cancel_body = (
                f"if [ \"$(cat {shlex.quote(cid_path.as_posix())} 2>/dev/null)\" = {cid} ] && "
                f"[ \"$({runtime} inspect --format '{{{{.Id}}}}' {name} 2>/dev/null)\" = {cid} ]; then "
                f"{runtime} rm -f {name} >/dev/null 2>&1 || true; "
                f"kill {shlex.quote(pid)} 2>/dev/null || true; "
                f"printf 'CANCELLED\\n' > {shlex.quote(cancelled_path.as_posix())}; "
                "printf 'CANCELLED\\n'; else printf 'UNKNOWN\\n'; exit 3; fi"
            )
        return (
            f"if {identity_check}; then {cancel_body}; "
            "else printf 'UNKNOWN\\n'; exit 3; fi"
        )

    def cancel(self, job_id: str, *, execute: bool = False) -> JobResult:
        spec = self._known_spec(job_id)
        previous = self._known_result(job_id)
        metadata = self._identity_metadata(spec, job_id, previous)
        if metadata is None:
            return JobResult(
                job_id=job_id,
                backend=self.name,
                status=JobStatus.UNKNOWN,
                executed=execute,
                plan=previous.plan,
                message="SSH job identity mismatch; cancellation refused",
                metadata=previous.metadata,
                created_at=previous.created_at,
            )
        argv = self._ssh_argv(self._cancel_script(spec, job_id, metadata))
        plan = self._plan(
            job_id=job_id,
            action="cancel",
            argv=argv,
            description=f"cancel SSH job {job_id}",
        )
        if not execute:
            return plan
        outcome = self._run(spec, argv, timeout=60)
        cancelled = outcome.return_code == 0 and "CANCELLED" in outcome.stdout
        status = JobStatus.CANCELLED if cancelled else JobStatus.UNKNOWN
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
                "SSH job cancelled"
                if cancelled
                else "SSH identity mismatch; cancellation refused"
            ),
            metadata=metadata,
            created_at=previous.created_at,
        )
        self._remember(job_id, result=result)
        return result

    def resume(self, job_id: str, *, execute: bool = False) -> JobResult:
        spec = self._known_spec(job_id)
        previous = self._known_result(job_id)
        attempt = int(previous.metadata.get("attempt") or 1) + 1
        argv = self._submit_argv(spec, job_id, attempt=attempt)
        nonce = self._attempt_nonce(spec, job_id, attempt)
        binding_digest = self._binding_digest(spec)
        plan = self._plan(
            job_id=job_id,
            action="resume",
            argv=argv,
            description=f"start fresh SSH attempt {attempt} for {job_id}",
            environment_keys=tuple(spec.env),
            metadata={
                "target": self.config.target,
                "remote_dir": self._remote_dir(job_id).as_posix(),
                "attempt": attempt,
                "identity_nonce": nonce,
                "binding_digest": binding_digest,
            },
            sensitive_values=self._sensitive_environment_values(spec.env),
        )
        if not execute:
            return plan
        current = self.status(job_id, execute=True)
        if current.metadata.get("cleanup_pending") is True:
            raise RuntimeError(
                f"cannot resume unresolved SSH launch {job_id}"
            )
        if current.status in {
            JobStatus.SUBMITTED,
            JobStatus.QUEUED,
            JobStatus.RUNNING,
            JobStatus.SUSPENDED,
        }:
            raise RuntimeError(f"cannot resume active SSH job {job_id}")
        self._reject_sensitive_remote_environment(spec)
        self._persist_submission_intent(spec, plan)
        transport_error: str | None = None
        try:
            outcome = self._run(spec, argv, timeout=60)
        except (subprocess.TimeoutExpired, OSError) as exc:
            transport_error = type(exc).__name__
            outcome = CommandOutcome(
                1,
                "",
                f"SSH resume transport error ({transport_error})\n",
            )
        identity_line = outcome.stdout.strip().splitlines()[-1] if outcome.stdout.strip() else ""
        remote_pid, _, remainder = identity_line.partition("|")
        remote_start, _, remote_container_id = remainder.partition("|")
        status = (
            JobStatus.SUBMITTED
            if outcome.return_code == 0
            and remote_pid.isdigit()
            and remote_start.isdigit()
            and (
                not spec.metadata.get("remote_source_sha256")
                or re.fullmatch(r"[0-9a-fA-F]{64}", remote_container_id)
            )
            else JobStatus.FAILED
        )
        cleanup_attempted = status is JobStatus.FAILED
        cleanup_status: str | None = None
        if cleanup_attempted:
            cleanup = self._cleanup_failed_launch(
                spec,
                job_id,
                attempt=attempt,
                nonce=nonce,
                binding_digest=binding_digest,
            )
            cleanup_status = self._cleanup_outcome_status(cleanup)
            outcome = CommandOutcome(
                outcome.return_code,
                outcome.stdout + cleanup.stdout,
                outcome.stderr + cleanup.stderr,
            )
        cleanup_pending = cleanup_attempted and cleanup_status != "CLEANED"
        if cleanup_pending:
            status = JobStatus.UNKNOWN
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
                "fresh SSH attempt submitted"
                if status is JobStatus.SUBMITTED
                else "SSH resume failed"
            ),
            metadata={
                "target": self.config.target,
                "remote_dir": self._remote_dir(job_id).as_posix(),
                "remote_pid": remote_pid or None,
                "remote_start_time": remote_start or None,
                "remote_container_id": remote_container_id or None,
                "remote_container_name": self._remote_container_name(job_id, attempt),
                "attempt": attempt,
                "identity_nonce": nonce,
                "binding_digest": binding_digest,
                "deadline_seconds": spec.resources.timeout_seconds,
                "submission_intent": cleanup_pending,
                "cleanup_attempted": cleanup_attempted,
                "cleanup_status": cleanup_status,
                "cleanup_pending": cleanup_pending,
                "transport_error": transport_error,
            },
            created_at=previous.created_at,
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
        previous = self._known_result(job_id)
        attempt = int(previous.metadata.get("attempt") or 1)
        log_path = self._remote_attempt_dir(job_id, attempt) / "job.log"
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
        return JobResult(
            job_id=job_id,
            backend=self.name,
            status=previous.status,
            executed=True,
            plan=plan.plan,
            return_code=outcome.return_code,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            message="SSH log snapshot",
            metadata=previous.metadata,
            created_at=previous.created_at,
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
        if any(
            character in pattern
            for pattern in selected
            for character in "*?[]"
        ):
            raise SSHSecurityError(
                "SSH artifact synchronization requires explicit file paths"
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
        local_root = Path(local_path).expanduser().absolute()
        if direction is ArtifactDirection.UPLOAD:
            local_root = safe_artifact_root(local_root, create=False)
            for pattern in selected:
                safe_artifact_file(
                    local_root,
                    pattern,
                    require_exists=True,
                )
        else:
            for pattern in selected:
                safe_artifact_destination(local_root, pattern)
        previous = self._known_result(job_id)
        raw_attempt = previous.metadata.get("attempt")
        attempt = (
            int(raw_attempt)
            if isinstance(raw_attempt, int) and not isinstance(raw_attempt, bool)
            else 1
        )
        remote_workdir = (
            self._remote_artifact_root(job_id, attempt)
            if spec.metadata.get("remote_source_sha256")
            else self._remote_workdir(spec, job_id)
        )
        staging_root = self._job_state_path(
            job_id,
            f"attempts/{attempt}/artifact-download",
        )
        commands: list[tuple[str, ...]] = []
        for pattern in selected:
            remote_path = (remote_workdir / pattern).as_posix()
            remote_arg = f"{self.config.target}:{shlex.quote(remote_path)}"
            if direction is ArtifactDirection.DOWNLOAD:
                commands.append(
                    self._ssh_argv(
                        self._remote_regular_file_check(
                            PurePosixPath(remote_path)
                        )
                    )
                )
                argv = (
                    *self.config.scp_base_argv(),
                    remote_arg,
                    str(staging_root / pattern),
                )
            else:
                commands.append(
                    self._ssh_argv(
                        self._remote_parent_prepare(
                            PurePosixPath(remote_path)
                        )
                    )
                )
                argv = (
                    *self.config.scp_base_argv(),
                    str(local_root / pattern),
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
        artifacts: tuple[ArtifactRecord, ...] = ()
        try:
            for command in commands:
                outcome = self._run(
                    spec,
                    command,
                    timeout=spec.resources.timeout_seconds,
                )
                stdout.append(outcome.stdout)
                stderr.append(outcome.stderr)
                if outcome.return_code != 0:
                    return_code = outcome.return_code
                    break
            if return_code == 0 and direction is ArtifactDirection.DOWNLOAD:
                artifacts = copy_local_artifacts(
                    source_root=staging_root,
                    destination_root=local_root,
                    patterns=selected,
                    attempt_id=attempt,
                )
            elif return_code == 0:
                artifacts = tuple(
                    file_record(
                        safe_artifact_file(
                            local_root,
                            pattern,
                            require_exists=True,
                        ),
                        display_path=pattern,
                        attempt_id=attempt,
                    )
                    for pattern in selected
                )
        except (OSError, ValueError) as exc:
            return_code = 1
            stderr.append(str(exc))
        finally:
            if direction is ArtifactDirection.DOWNLOAD and staging_root.exists():
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
            artifacts=artifacts,
            message=(
                "SSH artifact sync completed" if return_code == 0 else "SSH artifact sync failed"
            ),
            metadata={
                "paths": list(selected),
                "direction": direction.value,
                "attempt": attempt,
            },
        )


SSHComputeBackend = SSHBackend
