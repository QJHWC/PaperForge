from __future__ import annotations

import json
import os
import platform
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

from engine.secret_redaction import redact_secrets, redact_structure
from paperforge.path_safety import (
    is_link_or_reparse_point,
    reject_symlink_components,
    safe_mkdir,
)
from paperforge.policy import Action

from ._artifacts import artifact_patterns, copy_local_artifacts
from .base import ComputeBackend, JobStateError
from .contracts import (
    ArtifactDirection,
    JobResult,
    JobSpec,
    JobStatus,
    utc_now,
)


class LocalBackend(ComputeBackend):
    name = "local"
    policy_action = Action.LOCAL_EXECUTE
    _INHERITED_ENVIRONMENT = frozenset(
        {
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
            "PATH",
            "PYTHONPATH",
            "SYSTEMROOT",
            "TERM",
            "TZ",
            "WINDIR",
        }
    )

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._processes: dict[str, subprocess.Popen[bytes]] = {}
        self._log_paths: dict[str, Path] = {}

    @staticmethod
    def _sandbox_literal(path: Path) -> str:
        return str(path).replace("\\", "\\\\").replace('"', '\\"')

    @classmethod
    def _python_runtime_root(cls, executable: Path) -> Path | None:
        parts = executable.parts
        if ".pyenv" in parts and "versions" in parts:
            index = parts.index("versions")
            if len(parts) > index + 1:
                return Path(*parts[: index + 2])
        if "opt" in parts and "homebrew" in parts:
            return Path("/opt/homebrew")
        return None

    def _sandboxed_command(
        self,
        spec: JobSpec,
        *,
        workdir: Path,
        job_dir: Path,
        environment: dict[str, str],
        sandbox_nonce: str,
    ) -> tuple[list[str], dict[str, str]]:
        system = platform.system()
        if system == "Windows":
            return self._windows_sandboxed_command(
                spec,
                workdir=workdir,
                job_dir=job_dir,
                environment=environment,
                sandbox_nonce=sandbox_nonce,
            )
        if system == "Linux":
            return self._linux_sandboxed_command(
                spec,
                workdir=workdir,
                job_dir=job_dir,
                environment=environment,
            )
        if system != "Darwin":
            raise JobStateError(
                "local execution requires an operating-system filesystem sandbox"
            )
        sandbox = shutil.which("sandbox-exec")
        if sandbox is None:
            raise JobStateError("sandbox-exec is required for local execution")
        temporary = job_dir / "sandbox-tmp"
        home = temporary / "home"
        home.mkdir(parents=True, exist_ok=True)
        executable = Path(spec.command[0]).expanduser()
        resolved_executable = (
            executable.resolve(strict=True)
            if executable.is_absolute()
            else Path(shutil.which(str(executable)) or "").resolve(strict=True)
        )
        read_subpaths = {workdir, temporary}
        runtime_root = self._python_runtime_root(resolved_executable)
        if runtime_root is not None:
            read_subpaths.add(runtime_root)
        write_rules = []
        for raw_output in spec.outputs:
            if any(character in str(raw_output) for character in "*?[]"):
                raise JobStateError(
                    "executable local outputs must be explicit file paths"
                )
            output = (workdir / raw_output).absolute()
            if is_link_or_reparse_point(output) or (
                output != workdir and workdir not in output.parents
            ):
                raise JobStateError("local output leaves the sandbox worktree")
            output.parent.mkdir(parents=True, exist_ok=True)
            if output.exists() and not output.is_file():
                raise JobStateError("local output must be a regular file")
            output.touch(exist_ok=True)
            literal = self._sandbox_literal(output)
            write_rules.append(
                f'(allow file-write* (literal "{literal}") (subpath "{literal}"))'
            )
        profile = [
            "(version 1)",
            "(allow default)",
            "(deny network*)",
            "(deny file-write*)",
            '(deny file-read* (subpath "/Users"))',
            '(deny file-read* (subpath "/Volumes"))',
            '(deny file-read* (subpath "/private/tmp"))',
            (
                '(deny file-read* (subpath "'
                f'{self._sandbox_literal(Path(tempfile.gettempdir()).resolve())}'
                '"))'
            ),
            *[
                f'(allow file-read* (subpath "{self._sandbox_literal(path)}"))'
                for path in sorted(read_subpaths)
            ],
            f'(allow file-write* (subpath "{self._sandbox_literal(temporary)}"))',
            *write_rules,
        ]
        internal_state = workdir / ".paperforge"
        profile.append(
            f'(deny file-read* (subpath "{self._sandbox_literal(internal_state)}"))'
        )
        profile_path = job_dir / "sandbox.sb"
        profile_path.write_text("\n".join(profile) + "\n", encoding="utf-8")
        environment.update(
            {
                "HOME": str(home),
                "TMPDIR": str(temporary),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        return [
            sandbox,
            "-f",
            str(profile_path),
            "--",
            str(resolved_executable),
            *spec.command[1:],
        ], environment

    def _linux_sandboxed_command(
        self,
        spec: JobSpec,
        *,
        workdir: Path,
        job_dir: Path,
        environment: dict[str, str],
    ) -> tuple[list[str], dict[str, str]]:
        bubblewrap = shutil.which("bwrap")
        if bubblewrap is None:
            raise JobStateError(
                "bubblewrap (bwrap) is required for Linux local execution"
            )
        executable = Path(spec.command[0]).expanduser()
        executable_path = (
            executable.absolute()
            if executable.is_absolute()
            else Path(shutil.which(str(executable)) or "").absolute()
        )
        resolved_executable = (
            executable_path.resolve(strict=True)
        )
        if not resolved_executable.is_file():
            raise JobStateError("local executable could not be resolved")
        temporary = job_dir / "sandbox-tmp"
        temporary.mkdir(parents=True, exist_ok=True)
        empty_tmp = job_dir / "sandbox-root" / "tmp"
        (empty_tmp / "paperforge").mkdir(parents=True, exist_ok=True)
        read_roots = {
            str(Path(__file__).resolve().parents[2]),
            str(executable_path.parent.parent),
            str(resolved_executable.parent.parent),
        }
        for raw in environment.get("PYTHONPATH", "").split(os.pathsep):
            if raw:
                candidate = Path(raw).expanduser().resolve(strict=True)
                if candidate.is_dir():
                    read_roots.add(str(candidate))
        for system_root in ("/bin", "/lib", "/lib64", "/usr"):
            candidate = Path(system_root)
            if candidate.exists():
                read_roots.add(system_root)
        host_tmp = Path("/tmp")
        for raw_path in read_roots:
            candidate_path = Path(raw_path)
            try:
                relative_to_tmp = candidate_path.relative_to(host_tmp)
            except ValueError:
                continue
            (empty_tmp / relative_to_tmp).mkdir(parents=True, exist_ok=True)
        command: list[str] = [
            bubblewrap,
            "--die-with-parent",
            "--new-session",
            "--unshare-all",
            "--clearenv",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--ro-bind",
            str(empty_tmp),
            "/tmp",
            "--bind",
            str(temporary),
            "/tmp/paperforge",
            "--ro-bind",
            str(workdir),
            "/workspace",
        ]
        for binding_path in sorted(read_roots):
            command.extend(["--ro-bind", binding_path, binding_path])
        for raw_output in spec.outputs:
            if any(character in str(raw_output) for character in "*?[]"):
                raise JobStateError(
                    "executable local outputs must be explicit file paths"
                )
            output = (workdir / raw_output).absolute()
            if is_link_or_reparse_point(output) or (
                output != workdir and workdir not in output.parents
            ):
                raise JobStateError("local output leaves the sandbox worktree")
            output.parent.mkdir(parents=True, exist_ok=True)
            if output.exists() and not output.is_file():
                raise JobStateError("local output must be a regular file")
            output.touch(exist_ok=True)
            container_output = Path("/workspace") / raw_output
            command.extend(["--bind", str(output), str(container_output)])
        environment.update(
            {
                "HOME": "/tmp/paperforge/home",
                "TMPDIR": "/tmp/paperforge",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        mapped_python_path = []
        for raw_path in environment.get("PYTHONPATH", "").split(os.pathsep):
            path = Path(raw_path)
            try:
                mapped = Path("/workspace") / path.relative_to(workdir)
            except ValueError:
                mapped = path
            mapped_python_path.append(str(mapped))
        environment["PYTHONPATH"] = os.pathsep.join(mapped_python_path)
        for key, value in sorted(environment.items()):
            command.extend(["--setenv", key, value])
        command.extend(
            [
                "--chdir",
                "/workspace",
                "--",
                str(executable_path),
                *[
                    str(Path("/workspace") / Path(part).relative_to(workdir))
                    if Path(part).is_absolute()
                    and (
                        Path(part) == workdir
                        or workdir in Path(part).parents
                    )
                    else part
                    for part in spec.command[1:]
                ],
            ]
        )
        return command, environment

    def _windows_sandboxed_command(
        self,
        spec: JobSpec,
        *,
        workdir: Path,
        job_dir: Path,
        environment: dict[str, str],
        sandbox_nonce: str,
    ) -> tuple[list[str], dict[str, str]]:
        powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
        icacls = shutil.which("icacls.exe") or shutil.which("icacls")
        if powershell is None or icacls is None:
            raise JobStateError(
                "Windows local execution requires PowerShell and icacls"
            )
        del powershell, icacls
        executable = Path(spec.command[0]).expanduser()
        resolved_executable = (
            executable.resolve(strict=True)
            if executable.is_absolute()
            else Path(shutil.which(str(executable)) or "").resolve(strict=True)
        )
        if not resolved_executable.is_file():
            raise JobStateError("local executable could not be resolved")
        temporary = job_dir / "sandbox-tmp"
        home = temporary / "home"
        home.mkdir(parents=True, exist_ok=True)
        read_roots = {
            workdir,
            Path(__file__).resolve().parents[2],
            resolved_executable.parent.parent,
            Path(sys.base_prefix).resolve(strict=True),
            Path(sys.prefix).resolve(strict=True),
        }
        for raw in environment.get("PYTHONPATH", "").split(os.pathsep):
            if raw:
                candidate = Path(raw).expanduser().resolve(strict=True)
                if candidate.is_dir():
                    read_roots.add(candidate)
        write_roots = {temporary}
        for raw_output in spec.outputs:
            if any(character in str(raw_output) for character in "*?[]"):
                raise JobStateError(
                    "executable local outputs must be explicit file paths"
                )
            output = (workdir / raw_output).absolute()
            if is_link_or_reparse_point(output) or (
                output != workdir and workdir not in output.parents
            ):
                raise JobStateError("local output leaves the sandbox worktree")
            output.parent.mkdir(parents=True, exist_ok=True)
            if output.exists() and not output.is_file():
                raise JobStateError("local output must be a regular file")
            output.touch(exist_ok=True)
            write_roots.add(output)
        environment.update(
            {
                "HOME": str(home),
                "USERPROFILE": str(home),
                "LOCALAPPDATA": str(home / "AppData" / "Local"),
                "TEMP": str(temporary),
                "TMP": str(temporary),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        (home / "AppData" / "Local").mkdir(parents=True, exist_ok=True)
        config_path = job_dir / "windows-appcontainer.json"
        self._atomic_json(
            config_path,
            {
                "schema": "paperforge.windows-appcontainer/v1",
                "profile_name": f"PaperForge_{sandbox_nonce}",
                "command": [str(resolved_executable), *spec.command[1:]],
                "cwd": str(workdir),
                "environment": environment,
                "read_roots": [str(path) for path in sorted(read_roots)],
                "write_roots": [str(path) for path in sorted(write_roots)],
            },
        )
        return [
            sys.executable,
            "-m",
            "paperforge.compute._windows_appcontainer",
            str(config_path),
        ], environment

    @classmethod
    def _clean_environment(cls, spec: JobSpec) -> dict[str, str]:
        environment = {
            key: value
            for key, value in os.environ.items()
            if key in cls._INHERITED_ENVIRONMENT or key.startswith("LC_")
        }
        environment.update(spec.env)
        package_root = str(Path(__file__).resolve().parents[2])
        python_path = [
            package_root,
            *[
                str(Path(value).expanduser().resolve())
                for value in environment.get("PYTHONPATH", "").split(os.pathsep)
                if value
            ],
        ]
        environment["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(python_path))
        return environment

    def _atomic_json(self, path: Path, payload: dict[str, Any]) -> None:
        reject_symlink_components(path, anchor=self.state_dir)
        if is_link_or_reparse_point(path) or (
            path.exists() and not path.is_file()
        ):
            raise JobStateError(
                "local state target must be a regular file"
            )
        safe_payload = redact_structure(payload)
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                delete=False,
            ) as handle:
                json.dump(
                    safe_payload,
                    handle,
                    ensure_ascii=False,
                    sort_keys=True,
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
                temporary = handle.name
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)

    def _job_file(
        self,
        job_id: str,
        raw_path: object,
        expected_name: str,
    ) -> Path:
        job_dir = self._job_state_dir(job_id)
        candidate = Path(str(raw_path))
        reject_symlink_components(candidate, anchor=job_dir)
        expected = job_dir / expected_name
        if candidate.absolute() != expected.absolute():
            raise JobStateError(
                f"local state path is not the expected {expected_name}"
            )
        if is_link_or_reparse_point(candidate) or (
            candidate.exists() and not candidate.is_file()
        ):
            raise JobStateError(
                f"local state path is unsafe: {expected_name}"
            )
        return candidate

    def _prepare_attempt_spec(
        self,
        spec: JobSpec,
        *,
        job_id: str,
        attempt: int,
    ) -> tuple[JobSpec, Path]:
        """Materialize a fresh per-attempt tree without carrying old outputs."""

        source = Path(spec.workdir).expanduser().absolute()
        if is_link_or_reparse_point(source) or not source.is_dir():
            raise JobStateError("local execution source must be a safe directory")
        reject_symlink_components(source, anchor=Path(source.anchor))
        job_dir = self._job_state_dir(job_id)
        attempts_root = safe_mkdir(job_dir / "attempts", anchor=job_dir)
        attempt_root = attempts_root / str(attempt)
        reject_symlink_components(attempt_root, anchor=job_dir)
        if attempt_root.exists():
            if is_link_or_reparse_point(attempt_root) or not attempt_root.is_dir():
                raise JobStateError("local attempt path is unsafe")
            shutil.rmtree(attempt_root)
        attempt_root = safe_mkdir(attempt_root, anchor=job_dir)
        workspace = attempt_root / "workspace"
        temporary = Path(
            tempfile.mkdtemp(prefix=".workspace-", dir=attempt_root)
        )
        output_paths = {Path(path) for path in spec.outputs}
        if any(
            any(character in str(output) for character in "*?[]")
            for output in output_paths
        ):
            raise JobStateError(
                "executable local outputs must be explicit file paths"
            )
        try:
            for candidate in sorted(source.rglob("*")):
                relative = candidate.relative_to(source)
                if any(
                    relative == output or output in relative.parents
                    for output in output_paths
                ):
                    continue
                try:
                    candidate.absolute().relative_to(self.state_dir.absolute())
                except ValueError:
                    pass
                else:
                    continue
                if is_link_or_reparse_point(candidate):
                    raise JobStateError(
                        f"local execution source contains a symbolic link: {relative}"
                    )
                destination = temporary / relative
                if candidate.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                elif candidate.is_file():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(candidate, destination)
            temporary.replace(workspace)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

        for relative in output_paths:
            output = workspace / relative
            safe_mkdir(output.parent, anchor=workspace)
            reject_symlink_components(output, anchor=workspace)
            with output.open("wb"):
                pass

        payload = spec.to_dict()
        payload["workdir"] = str(workspace)
        remapped_command = [str(spec.command[0])]
        for argument in spec.command[1:]:
            candidate = Path(argument)
            if candidate.is_absolute():
                with suppress(ValueError):
                    argument = str(workspace / candidate.relative_to(source))
            remapped_command.append(str(argument))
        payload["command"] = remapped_command
        return JobSpec.from_dict(payload), workspace

    def _attempt_workspace(
        self,
        job_id: str,
        metadata: dict[str, Any],
    ) -> tuple[Path, int]:
        attempt = metadata.get("attempt")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise JobStateError("local attempt identity is missing")
        job_dir = self._job_state_dir(job_id)
        expected = job_dir / "attempts" / str(attempt) / "workspace"
        raw = metadata.get("attempt_workdir")
        candidate = Path(str(raw)).expanduser().absolute()
        reject_symlink_components(candidate, anchor=job_dir)
        if candidate != expected.absolute() or not candidate.is_dir():
            raise JobStateError("local attempt workspace failed identity verification")
        return candidate, attempt

    def _start_supervisor(
        self,
        spec: JobSpec,
        *,
        job_id: str,
        job_dir: Path,
        log_path: Path,
        append: bool,
        attempt: int,
    ) -> tuple[subprocess.Popen[bytes], dict[str, Any]]:
        environment = self._clean_environment(spec)
        nonce = secrets.token_hex(24)
        sandboxed_command, environment = self._sandboxed_command(
            spec,
            workdir=Path(spec.workdir).expanduser().resolve(),
            job_dir=job_dir,
            environment=environment,
            sandbox_nonce=nonce,
        )
        launch_path = job_dir / "launch.json"
        identity_path = job_dir / "supervisor.json"
        completion_path = job_dir / "completion.json"
        identity_path.unlink(missing_ok=True)
        completion_path.unlink(missing_ok=True)
        self._atomic_json(
            launch_path,
            {
                "schema": "paperforge.local-launch/v1",
                "nonce": nonce,
                "command": sandboxed_command,
                "cwd": str(Path(spec.workdir).expanduser().resolve()),
                "environment": environment,
                "log_path": str(log_path),
                "identity_path": str(identity_path),
                "completion_path": str(completion_path),
                "timeout_seconds": spec.resources.timeout_seconds,
                "append": append,
                "attempt": attempt,
            },
        )
        supervisor_environment = self._clean_environment(spec)
        supervisor_command = [
            sys.executable,
            "-m",
            "paperforge.compute._local_supervisor",
            str(launch_path),
        ]
        process_options: dict[str, Any] = {
            "cwd": job_dir,
            "env": supervisor_environment,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if os.name == "nt":
            process_options["creationflags"] = getattr(
                subprocess,
                "CREATE_NEW_PROCESS_GROUP",
                0x00000200,
            )
        else:
            process_options["start_new_session"] = True
        process = subprocess.Popen(supervisor_command, **process_options)
        identity_deadline = time.monotonic() + 1
        while (
            not identity_path.is_file()
            and process.poll() is None
            and time.monotonic() < identity_deadline
        ):
            time.sleep(0.01)
        if not identity_path.is_file() and process.poll() is not None:
            raise JobStateError("local supervisor failed during startup")
        return process, {
            "pid": process.pid,
            "supervisor_pid": process.pid,
            "supervisor_nonce": nonce,
            "launch_path": str(launch_path),
            "identity_path": str(identity_path),
            "completion_path": str(completion_path),
            "log_path": str(log_path),
            "workdir": str(Path(spec.workdir).expanduser().resolve()),
            "attempt_workdir": str(Path(spec.workdir).expanduser().resolve()),
            "attempt": attempt,
        }

    @staticmethod
    def _load_json(path: Path) -> dict[str, Any] | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _completion_result(
        self,
        job_id: str,
        previous: JobResult,
    ) -> JobResult | None:
        metadata = dict(previous.metadata)
        try:
            completion_path = self._job_file(
                job_id,
                metadata.get("completion_path"),
                "completion.json",
            )
        except (JobStateError, ValueError):
            return JobResult(
                job_id=job_id,
                backend=self.name,
                status=JobStatus.FAILED,
                executed=True,
                plan=previous.plan,
                message="local completion path failed identity verification",
                metadata=metadata,
                created_at=previous.created_at,
                updated_at=utc_now(),
            )
        if not completion_path.is_file():
            return None
        completion = self._load_json(completion_path)
        if (
            completion is None
            or completion.get("schema") != "paperforge.local-completion/v1"
            or completion.get("nonce") != metadata.get("supervisor_nonce")
        ):
            return JobResult(
                job_id=job_id,
                backend=self.name,
                status=JobStatus.FAILED,
                executed=True,
                plan=previous.plan,
                message="local completion record failed identity verification",
                metadata=metadata,
                created_at=previous.created_at,
                updated_at=utc_now(),
            )
        status = JobStatus(str(completion.get("status")))
        raw_return_code = completion.get("return_code")
        return JobResult(
            job_id=job_id,
            backend=self.name,
            status=status,
            executed=True,
            plan=previous.plan,
            return_code=(
                int(raw_return_code) if raw_return_code is not None else None
            ),
            message=str(completion.get("message") or ""),
            metadata=metadata,
            created_at=previous.created_at,
            updated_at=utc_now(),
        )

    def _supervisor_alive(self, job_id: str, metadata: dict[str, Any]) -> bool:
        process = self._processes.get(job_id)
        if process is not None:
            return process.poll() is None
        raw_pid = metadata.get("supervisor_pid")
        if not isinstance(raw_pid, int) or raw_pid < 2:
            return False
        try:
            identity_path = self._job_file(
                job_id,
                metadata.get("identity_path"),
                "supervisor.json",
            )
        except (JobStateError, ValueError):
            return False
        identity = self._load_json(identity_path)
        if (
            identity is None
            or identity.get("schema") != "paperforge.local-supervisor/v1"
            or identity.get("pid") != raw_pid
            or identity.get("nonce") != metadata.get("supervisor_nonce")
            or identity.get("launch_path") != metadata.get("launch_path")
        ):
            return False
        if os.name == "nt":
            powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
            if powershell is None:
                return False
            inspected = subprocess.run(
                [
                    powershell,
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    (
                        "$p = Get-CimInstance Win32_Process -Filter "
                        f"\"ProcessId = {raw_pid}\"; "
                        "if ($null -ne $p) { [Console]::Out.Write($p.CommandLine) }"
                    ),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            command = inspected.stdout.strip()
            return (
                inspected.returncode == 0
                and "paperforge.compute._local_supervisor" in command
                and str(metadata["launch_path"]) in command
            )
        try:
            os.kill(raw_pid, 0)
        except (OSError, ProcessLookupError):
            return False
        inspected = subprocess.run(
            ["ps", "-ww", "-p", str(raw_pid), "-o", "command="],
            check=False,
            capture_output=True,
            text=True,
        )
        command = inspected.stdout.strip()
        return (
            inspected.returncode == 0
            and "paperforge.compute._local_supervisor" in command
            and str(metadata["launch_path"]) in command
        )

    @staticmethod
    def _signal_supervisor(pid: int, *, force: bool) -> None:
        if os.name != "nt":
            os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
            return
        if force:
            subprocess.run(
                ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
                check=False,
                capture_output=True,
                text=True,
            )
            return
        os.kill(
            pid,
            getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM),
        )

    def _recover_submission_intent(
        self,
        job_id: str,
        previous: JobResult,
    ) -> JobResult:
        job_dir = self._job_state_dir(job_id)
        launch_path = job_dir / "launch.json"
        identity_path = job_dir / "supervisor.json"
        launch = self._load_json(launch_path)
        identity = self._load_json(identity_path)
        if (
            launch is None
            or identity is None
            or identity.get("schema")
            != "paperforge.local-supervisor/v1"
            or identity.get("nonce") != launch.get("nonce")
            or identity.get("launch_path") != str(launch_path)
            or not isinstance(identity.get("pid"), int)
        ):
            return previous
        metadata = {
            **dict(previous.metadata),
            "pid": identity["pid"],
            "supervisor_pid": identity["pid"],
            "supervisor_nonce": launch["nonce"],
            "launch_path": str(launch_path),
            "identity_path": str(identity_path),
            "completion_path": str(job_dir / "completion.json"),
            "log_path": str(job_dir / "job.log"),
            "workdir": str(launch.get("cwd") or ""),
            "attempt_workdir": str(launch.get("cwd") or ""),
            "attempt": int(launch.get("attempt") or 1),
            "submission_intent": False,
            "recovered_from_intent": True,
        }
        return JobResult(
            job_id=job_id,
            backend=self.name,
            status=JobStatus.RUNNING,
            executed=True,
            plan=previous.plan,
            message="local supervisor recovered from submission intent",
            metadata=metadata,
            created_at=previous.created_at,
            updated_at=utc_now(),
        )

    def submit(self, spec: JobSpec, *, execute: bool | None = None) -> JobResult:
        job_id = self._job_id(spec)
        plan = self._plan(
            job_id=job_id,
            action="submit",
            argv=spec.command,
            description=f"start local job {job_id}",
            cwd=spec.workdir,
            environment_keys=tuple(spec.env),
            sensitive_values=self._sensitive_environment_values(spec.env),
        )
        self._remember(job_id, spec=spec, result=plan)
        if not self._should_execute(spec, execute):
            return plan

        self.policy.validate_command(spec.command, self.policy_action)
        job_dir = self._job_state_dir(job_id)
        attempt_spec, _ = self._prepare_attempt_spec(
            spec,
            job_id=job_id,
            attempt=1,
        )
        self._persist_submission_intent(spec, plan)
        log_path = job_dir / "job.log"
        process, metadata = self._start_supervisor(
            attempt_spec,
            job_id=job_id,
            job_dir=job_dir,
            log_path=log_path,
            append=False,
            attempt=1,
        )
        result = JobResult(
            job_id,
            backend=self.name,
            status=JobStatus.RUNNING,
            executed=True,
            plan=plan.plan,
            message="local supervisor started",
            metadata=metadata,
            created_at=plan.created_at,
        )
        with self._lock:
            self._processes[job_id] = process
            self._log_paths[job_id] = log_path
        try:
            self._remember(job_id, result=result)
        except Exception:
            with suppress(OSError, ProcessLookupError):
                self._signal_supervisor(process.pid, force=True)
            raise
        return result

    def status(self, job_id: str, *, execute: bool = False) -> JobResult:
        plan = self._plan(
            job_id=job_id,
            action="status",
            argv=("paperforge-local-status", job_id),
            description=f"inspect local job {job_id}",
        )
        if not execute:
            return plan

        previous = self._known_result(job_id)
        if previous.status.terminal:
            return previous
        if previous.metadata.get("submission_intent") is True:
            recovered = self._recover_submission_intent(
                job_id,
                previous,
            )
            if recovered is not previous:
                previous = recovered
                self._remember(job_id, result=previous)
        completion = self._completion_result(job_id, previous)
        if completion is not None:
            self._remember(job_id, result=completion)
            return completion
        if self._supervisor_alive(job_id, dict(previous.metadata)):
            return previous
        process = self._processes.get(job_id)
        if process is not None and process.poll() is not None:
            time.sleep(0.05)
            completion = self._completion_result(job_id, previous)
            if completion is not None:
                self._remember(job_id, result=completion)
                return completion
        result = JobResult(
            job_id=job_id,
            backend=self.name,
            status=JobStatus.FAILED,
            executed=True,
            plan=previous.plan,
            message="local supervisor exited without a completion record",
            metadata=previous.metadata,
            created_at=previous.created_at,
            updated_at=utc_now(),
        )
        self._remember(job_id, result=result)
        return result

    def cancel(self, job_id: str, *, execute: bool = False) -> JobResult:
        plan = self._plan(
            job_id=job_id,
            action="cancel",
            argv=("paperforge-local-cancel", job_id),
            description=f"terminate local job {job_id}",
        )
        if not execute:
            return plan

        spec = self._known_spec(job_id)
        self.policy.validate_command(spec.command, self.policy_action)
        previous = self.status(job_id, execute=True)
        if previous.status.terminal:
            return previous
        metadata = dict(previous.metadata)
        raw_pid = metadata.get("supervisor_pid")
        if not self._supervisor_alive(job_id, metadata) or not isinstance(
            raw_pid, int
        ):
            return self.status(job_id, execute=True)
        with suppress(OSError, ProcessLookupError):
            self._signal_supervisor(raw_pid, force=False)
        deadline = time.monotonic() + 6
        current = previous
        while time.monotonic() < deadline:
            time.sleep(0.05)
            current = self.status(job_id, execute=True)
            if current.status.terminal:
                return current
        if self._supervisor_alive(job_id, metadata):
            with suppress(OSError, ProcessLookupError):
                self._signal_supervisor(raw_pid, force=True)
        result = JobResult(
            job_id=job_id,
            backend=self.name,
            status=JobStatus.FAILED,
            executed=True,
            plan=plan.plan,
            message="local supervisor did not acknowledge cancellation",
            metadata=previous.metadata,
            created_at=previous.created_at,
        )
        self._remember(job_id, result=result)
        return result

    def resume(self, job_id: str, *, execute: bool = False) -> JobResult:
        spec = self._known_spec(job_id)
        plan = self._plan(
            job_id=job_id,
            action="resume",
            argv=spec.command,
            description=f"restart local job {job_id}",
            cwd=spec.workdir,
            environment_keys=tuple(spec.env),
            sensitive_values=self._sensitive_environment_values(spec.env),
        )
        if not execute:
            return plan

        current = self.status(job_id, execute=True)
        if current.status in {JobStatus.RUNNING, JobStatus.SUBMITTED, JobStatus.QUEUED}:
            raise JobStateError(f"cannot resume active local job {job_id}")
        self.policy.validate_command(spec.command, self.policy_action)
        job_dir = self._job_state_dir(job_id)
        log_path = job_dir / "job.log"
        reject_symlink_components(log_path, anchor=job_dir)
        if is_link_or_reparse_point(log_path) or (
            log_path.exists() and not log_path.is_file()
        ):
            raise JobStateError("local log path is unsafe")
        with log_path.open("a", encoding="utf-8") as log_handle:
            log_handle.write("\n--- resumed ---\n")
        attempt = int(current.metadata.get("attempt", 1)) + 1
        attempt_spec, _ = self._prepare_attempt_spec(
            spec,
            job_id=job_id,
            attempt=attempt,
        )
        process, metadata = self._start_supervisor(
            attempt_spec,
            job_id=job_id,
            job_dir=log_path.parent,
            log_path=log_path,
            append=True,
            attempt=attempt,
        )
        result = JobResult(
            job_id=job_id,
            backend=self.name,
            status=JobStatus.RUNNING,
            executed=True,
            plan=plan.plan,
            message="local job resumed",
            metadata=metadata,
            created_at=current.created_at,
        )
        with self._lock:
            self._processes[job_id] = process
            self._log_paths[job_id] = log_path
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
        argv: tuple[str, ...] = ("paperforge-local-logs", job_id)
        if tail is not None:
            argv += ("--tail", str(tail))
        if follow:
            argv += ("--follow",)
        plan = self._plan(
            job_id=job_id,
            action="logs",
            argv=argv,
            description=f"read local job log for {job_id}",
        )
        if not execute:
            return plan
        if follow:
            raise ValueError("follow=True is not supported for finite API responses")
        previous = self.status(job_id, execute=True)
        raw_log_path = previous.metadata.get("log_path")
        try:
            log_path = (
                self._job_file(job_id, raw_log_path, "job.log")
                if raw_log_path
                else None
            )
        except (JobStateError, ValueError):
            log_path = None
        if log_path is None or not log_path.exists():
            content = ""
        else:
            content = log_path.read_text(encoding="utf-8", errors="replace")
        if tail is not None:
            content = "".join(content.splitlines(keepends=True)[-tail:])
        spec = self._known_spec(job_id)
        content = redact_secrets(
            content,
            secret_values=self._sensitive_environment_values(spec.env),
        )
        return JobResult(
            job_id=job_id,
            backend=self.name,
            status=previous.status,
            executed=True,
            plan=plan.plan,
            return_code=previous.return_code,
            stdout=content,
            message="local job log snapshot",
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
        previous = self._known_result(job_id)
        metadata = dict(previous.metadata)
        try:
            workdir, attempt = self._attempt_workspace(job_id, metadata)
        except JobStateError:
            workdir = Path(spec.workdir).expanduser().absolute()
            attempt = None
        local_root = Path(local_path).expanduser().absolute()
        if direction is ArtifactDirection.DOWNLOAD:
            source_root, destination_root = workdir, local_root
        else:
            source_root, destination_root = local_root, workdir
        plan = self._plan(
            job_id=job_id,
            action="artifact-sync",
            argv=(
                "paperforge-local-copy",
                direction.value,
                str(source_root),
                str(destination_root),
                *selected,
            ),
            description=f"{direction.value} artifacts for local job {job_id}",
        )
        if not execute:
            return plan

        self.policy.require(self.policy_action, detail=f"artifact sync {job_id}")
        try:
            artifacts = copy_local_artifacts(
                source_root=source_root,
                destination_root=destination_root,
                patterns=selected,
                attempt_id=attempt,
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
                message="artifact sync failed",
            )
        return JobResult(
            job_id=job_id,
            backend=self.name,
            status=JobStatus.SUCCEEDED,
            executed=True,
            plan=plan.plan,
            return_code=0,
            artifacts=artifacts,
            message=f"synced {len(artifacts)} artifact files",
        )


LocalComputeBackend = LocalBackend
