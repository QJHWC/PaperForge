"""SSH remote runner for PaperForge cloud phase.

Provides upload / execute / download over SSH+SFTP so that experiment code
can be sent to a GPU server, trained remotely, and results pulled back for
paper backfill — all without manual scp/rsync steps.
"""

from __future__ import annotations

import fnmatch
import os
import os.path as osp
import stat
import sys
import time
from pathlib import Path
from typing import Any

import yaml

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import contextlib

from engine.secret_redaction import redact_secrets
from paperforge.policy import Action, ExecutionPolicy, PolicyViolation

SECRET_UPLOAD_DENYLIST = frozenset(
    {
        ".env",
        ".env*",
        ".aws",
        ".azure",
        ".gnupg",
        ".kube",
        ".netrc",
        ".npmrc",
        ".pypirc",
        ".secrets*",
        ".ssh",
        "credentials",
        "credentials.*",
        "*credentials*.json",
        "*credentials*.yaml",
        "*credentials*.yml",
        "*service-account*.json",
        "*service_account*.json",
        "secrets",
        "secrets.*",
        "*secrets*.json",
        "*secrets*.yaml",
        "*secrets*.yml",
        "*.jks*",
        "*.kdbx*",
        "*.key*",
        "*.keystore*",
        "*.p12*",
        "*.pem*",
        "*.pfx*",
        "id_dsa*",
        "id_ecdsa*",
        "id_ed25519*",
        "id_rsa*",
        "key*.sh*",
        "remote*.yaml*",
        "remote*.yml*",
    }
)
MAX_REMOTE_OUTPUT_BYTES = 8 * 1024 * 1024


def _lazy_import_paramiko():
    try:
        import paramiko
        return paramiko
    except ImportError:
        raise ImportError(
            "paramiko is required for remote execution. "
            "Install it with: pip install paramiko"
        ) from None


def _resolve_env(value: Any) -> str:
    """Resolve $ENV_VAR references in string values."""
    if not isinstance(value, str):
        return str(value) if value is not None else ""
    if value.startswith("$"):
        return os.environ.get(value[1:], "")
    return value


def load_remote_config(config_path: str) -> dict:
    with open(config_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    auth = cfg.get("auth", {})
    for key in ("password", "passphrase"):
        if key in auth:
            auth[key] = _resolve_env(auth[key])

    configured_known_hosts = (
        cfg.get("known_hosts_file") or cfg.get("known_hosts")
    )
    defaults = {
        "host": "",
        "port": 22,
        "username": "paperforge",
        "known_hosts_file": "~/.ssh/known_hosts",
        "auth": {"method": "key", "key_path": "~/.ssh/id_ed25519"},
        "local_upload_root": str(
            Path(config_path).expanduser().resolve().parent
        ),
        "upload_paths": [],
        "upload_excludes": ["__pycache__", ".git", "*.pyc", ".DS_Store"],
        "train_command": "",
        "results_dir": "",
        "download_excludes": ["__pycache__", "*.pyc", ".git"],
        "poll_interval_seconds": 30,
        "connect_timeout": 15,
        "max_remote_output_bytes": MAX_REMOTE_OUTPUT_BYTES,
    }
    for k, v in defaults.items():
        cfg.setdefault(k, v)
    if configured_known_hosts:
        cfg["known_hosts_file"] = configured_known_hosts
    cfg["known_hosts_file"] = _resolve_env(cfg["known_hosts_file"])
    cfg.setdefault(
        "remote_workdir",
        f"/home/{cfg['username']}/experiment",
    )

    if not cfg["host"]:
        raise ValueError("remote config: 'host' is required")
    if not cfg["username"]:
        raise ValueError("remote config: 'username' is required")
    if not cfg["known_hosts_file"]:
        raise ValueError("remote config: 'known_hosts_file' is required")
    if not cfg["train_command"]:
        raise ValueError("remote config: 'train_command' is required")
    if not cfg["results_dir"]:
        raise ValueError("remote config: 'results_dir' is required")

    return cfg


class RemoteRunner:
    """Manages SSH connection, file transfer, and remote command execution."""

    def __init__(self, config: dict):
        self.cfg = config
        self.paramiko = _lazy_import_paramiko()
        self._client: Any = None
        self._sftp: Any = None

    # ── Connection ──────────────────────────────────────────────

    def connect(self) -> None:
        host = redact_secrets(self.cfg["host"])
        username = redact_secrets(self.cfg["username"])
        print(
            f"[remote] connecting to {host}:{self.cfg['port']} "
            f"as {username} ..."
        )
        client = self.paramiko.SSHClient()
        known_hosts_value = (
            self.cfg.get("known_hosts_file")
            or self.cfg.get("known_hosts")
            or "~/.ssh/known_hosts"
        )
        known_hosts = Path(str(known_hosts_value)).expanduser()
        try:
            known_hosts = known_hosts.resolve(strict=True)
        except (FileNotFoundError, OSError) as exc:
            safe_path = redact_secrets(known_hosts)
            raise FileNotFoundError(
                "remote config: pinned known_hosts file not found: "
                f"{safe_path}"
            ) from exc
        if not known_hosts.is_file():
            raise ValueError(
                "remote config: pinned known_hosts must be a file"
            )
        client.load_host_keys(str(known_hosts))
        client.set_missing_host_key_policy(self.paramiko.RejectPolicy())

        auth = self.cfg.get("auth", {})
        method = auth.get("method", "key")
        kwargs: dict[str, Any] = {
            "hostname": self.cfg["host"],
            "port": int(self.cfg["port"]),
            "username": self.cfg["username"],
            "timeout": self.cfg.get("connect_timeout", 15),
        }

        if method == "key":
            key_path = osp.expanduser(
                auth.get("key_path", "~/.ssh/id_ed25519")
            )
            passphrase = auth.get("passphrase") or None
            kwargs["key_filename"] = key_path
            if passphrase:
                kwargs["passphrase"] = passphrase
        elif method == "password":
            kwargs["password"] = auth["password"]
        else:
            raise ValueError(f"unsupported auth method: {method}")

        client.connect(**kwargs)
        self._client = client
        self._sftp = client.open_sftp()
        print("[remote] connected")

    def close(self) -> None:
        if self._sftp:
            self._sftp.close()
            self._sftp = None
        if self._client:
            self._client.close()
            self._client = None
        print("[remote] disconnected")

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    # ── Upload ──────────────────────────────────────────────────

    def _should_exclude(self, name: str, excludes: list[str]) -> bool:
        return any(fnmatch.fnmatch(name, pat) for pat in excludes)

    def _is_secret_upload_name(self, name: str) -> bool:
        normalized = name.casefold()
        return any(
            fnmatch.fnmatch(normalized, pattern.casefold())
            for pattern in SECRET_UPLOAD_DENYLIST
        )

    def _upload_candidate_allowed(self, candidate: Path, root: Path) -> bool:
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
            candidate_relative = candidate.relative_to(root)
            resolved_relative = resolved.relative_to(root)
        except (FileNotFoundError, OSError, ValueError):
            return False

        return not any(
            self._is_secret_upload_name(part)
            for part in (*candidate_relative.parts, *resolved_relative.parts)
        )

    def _print_upload_skip(self, path: Path, reason: str) -> None:
        safe_path = redact_secrets(path)
        print(f"[remote][upload] skip {reason}: {safe_path}")

    def _sftp_mkdir_p(self, remote_dir: str) -> None:
        dirs_to_create = []
        current = remote_dir
        while True:
            try:
                self._sftp.stat(current)
                break
            except FileNotFoundError:
                dirs_to_create.append(current)
                parent = osp.dirname(current)
                if parent == current:
                    break
                current = parent

        for d in reversed(dirs_to_create):
            with contextlib.suppress(OSError):
                self._sftp.mkdir(d)

    def upload(self) -> int:
        """Upload local paths to remote_workdir. Returns file count."""
        remote_base = self.cfg["remote_workdir"]
        excludes = self.cfg.get("upload_excludes", [])
        upload_paths = self.cfg.get("upload_paths", [])

        if not upload_paths:
            print("[remote][upload] no upload_paths configured, skipping")
            return 0

        self._sftp_mkdir_p(remote_base)
        count = 0

        for local_path_str in upload_paths:
            requested_path = Path(local_path_str).expanduser()
            if not requested_path.is_absolute():
                requested_path = Path.cwd() / requested_path
            if requested_path.is_symlink():
                self._print_upload_skip(requested_path, "symlinked root")
                continue
            try:
                local_path = requested_path.resolve(strict=True)
            except (FileNotFoundError, OSError):
                self._print_upload_skip(requested_path, "missing")
                continue
            if self._should_exclude(local_path.name, excludes):
                self._print_upload_skip(local_path, "excluded")
                continue
            if self._is_secret_upload_name(local_path.name):
                self._print_upload_skip(local_path, "protected path")
                continue

            if local_path.is_file():
                remote_file = f"{remote_base}/{local_path.name}"
                print(
                    f"  {redact_secrets(local_path)} -> "
                    f"{redact_secrets(remote_file)}"
                )
                self._sftp.put(str(local_path), remote_file)
                count += 1
            elif local_path.is_dir():
                count += self._upload_dir(
                    str(local_path), remote_base, excludes
                )

        print(f"[remote][upload] {count} files uploaded")
        return count

    def _upload_dir(
        self, local_dir: str, remote_base: str, excludes: list[str]
    ) -> int:
        requested_root = Path(local_dir).expanduser()
        if requested_root.is_symlink():
            self._print_upload_skip(requested_root, "symlinked root")
            return 0
        try:
            local_root = requested_root.resolve(strict=True)
        except (FileNotFoundError, OSError):
            self._print_upload_skip(requested_root, "missing")
            return 0
        if self._is_secret_upload_name(local_root.name):
            self._print_upload_skip(local_root, "protected path")
            return 0

        count = 0
        dir_name = local_root.name
        remote_root = f"{remote_base}/{dir_name}"
        self._sftp_mkdir_p(remote_root)

        for dirpath, dirnames, filenames in os.walk(
            str(local_root),
            followlinks=False,
        ):
            current_dir = Path(dirpath)
            allowed_dirs: list[str] = []
            for dirname in dirnames:
                candidate = current_dir / dirname
                if self._should_exclude(dirname, excludes):
                    continue
                if candidate.is_symlink():
                    self._print_upload_skip(candidate, "symlinked directory")
                    continue
                if not self._upload_candidate_allowed(candidate, local_root):
                    self._print_upload_skip(candidate, "protected path")
                    continue
                allowed_dirs.append(dirname)
            dirnames[:] = allowed_dirs

            rel = current_dir.relative_to(local_root).as_posix() or "."
            remote_dir = (
                f"{remote_root}/{rel}" if rel != "." else remote_root
            )
            self._sftp_mkdir_p(remote_dir)

            for fname in filenames:
                if self._should_exclude(fname, excludes):
                    continue
                local_path = current_dir / fname
                if not self._upload_candidate_allowed(local_path, local_root):
                    self._print_upload_skip(local_path, "protected path")
                    continue
                local_file = str(local_path)
                remote_file = f"{remote_dir}/{fname}"
                print(
                    f"  {redact_secrets(local_file)} -> "
                    f"{redact_secrets(remote_file)}"
                )
                self._sftp.put(local_file, remote_file)
                count += 1

        return count

    # ── Remote Execution ────────────────────────────────────────

    def _validate_remote_command(
        self,
        command: str,
    ) -> tuple[str, tuple[str, ...]]:
        auth = self.cfg.get("auth", {})
        secret_values = tuple(
            str(value)
            for value in (auth.get("password"), auth.get("passphrase"))
            if value
        )
        safe_command = redact_secrets(
            command,
            secret_values=secret_values,
        )
        configured_profile = self.cfg.get("execution_profile")
        inherited_profile = os.getenv("PAPERFORGE_EXECUTION_PROFILE")
        if configured_profile is not None and inherited_profile is not None:
            configured_policy = ExecutionPolicy.from_value(
                configured_profile
            )
            inherited_policy = ExecutionPolicy.from_value(inherited_profile)
            if configured_policy.profile is not inherited_policy.profile:
                raise PolicyViolation(
                    "conflicting execution profiles for remote process"
                )
        policy = ExecutionPolicy.from_value(
            configured_profile or inherited_profile
        )
        policy.validate_command([safe_command], Action.REMOTE_EXECUTE)
        return safe_command, secret_values

    def run_command(self, command: str | None = None) -> int:
        """Execute a command on the remote server.

        Buffers and redacts combined stdout/stderr. Returns the exit code.
        """
        cmd = command or self.cfg["train_command"]
        safe_cmd, secret_values = self._validate_remote_command(cmd)
        print(f"[remote][exec] {safe_cmd}")

        transport = self._client.get_transport()
        channel = transport.open_session()
        channel.set_combine_stderr(True)
        channel.exec_command(cmd)

        output = bytearray()
        while True:
            if channel.recv_ready():
                data = channel.recv(4096)
                if data:
                    output.extend(
                        data if isinstance(data, bytes) else str(data).encode()
                    )
            if channel.exit_status_ready():
                while channel.recv_ready():
                    data = channel.recv(4096)
                    if data:
                        output.extend(
                            data
                            if isinstance(data, bytes)
                            else str(data).encode()
                        )
                break
            time.sleep(0.1)

        exit_code = channel.recv_exit_status()
        channel.close()
        rendered_output = redact_secrets(
            output.decode("utf-8", errors="replace"),
            secret_values=secret_values,
        )
        if rendered_output:
            sys.stdout.write(rendered_output)
            sys.stdout.flush()
        separator = "" if rendered_output.endswith("\n") else "\n"
        print(f"{separator}[remote][exec] exit code: {exit_code}")
        return exit_code

    # ── Download ────────────────────────────────────────────────

    def download(self, local_dest: str) -> int:
        """Download results_dir to local_dest and return the file count."""
        remote_dir = self.cfg["results_dir"]
        excludes = self.cfg.get("download_excludes", [])
        local_dest_path = Path(local_dest)
        local_dest_path.mkdir(parents=True, exist_ok=True)

        print(
            f"[remote][download] {redact_secrets(remote_dir)} -> "
            f"{redact_secrets(local_dest)}"
        )
        count = self._download_dir(remote_dir, str(local_dest_path), excludes)
        print(f"[remote][download] {count} files downloaded")
        return count

    def _download_dir(
        self, remote_dir: str, local_dir: str, excludes: list[str]
    ) -> int:
        count = 0
        try:
            entries = self._sftp.listdir_attr(remote_dir)
        except FileNotFoundError:
            print(
                "[remote][download] remote dir not found: "
                f"{redact_secrets(remote_dir)}"
            )
            return 0

        os.makedirs(local_dir, exist_ok=True)

        for entry in entries:
            name = entry.filename
            if self._should_exclude(name, excludes):
                continue

            remote_path = f"{remote_dir}/{name}"
            local_path = osp.join(local_dir, name)

            if stat.S_ISDIR(entry.st_mode):
                count += self._download_dir(remote_path, local_path, excludes)
            else:
                print(
                    f"  {redact_secrets(remote_path)} -> "
                    f"{redact_secrets(local_path)}"
                )
                self._sftp.get(remote_path, local_path)
                count += 1

        return count

    # ── Full Cycle ──────────────────────────────────────────────

    def run_full_cycle(self, local_download_dir: str) -> int:
        """Upload -> execute -> download. Returns remote exit code."""
        self._validate_remote_command(self.cfg["train_command"])
        self.upload()
        exit_code = self.run_command()
        if exit_code != 0:
            print(
                f"[remote] WARNING: training exited with code {exit_code}. "
                "Downloading available results anyway."
            )
        self.download(local_download_dir)
        return exit_code


# ── CLI entry point (standalone testing) ────────────────────────

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="PaperForge SSH remote runner (standalone)"
    )
    parser.add_argument(
        "--config", required=True, help="Path to remote.yaml"
    )
    parser.add_argument(
        "--download-dir",
        default="./remote_results",
        help="Local directory for downloaded results",
    )
    parser.add_argument(
        "--upload-only",
        action="store_true",
        help="Only upload, skip exec and download",
    )
    parser.add_argument(
        "--download-only",
        action="store_true",
        help="Only download, skip upload and exec",
    )
    parser.add_argument(
        "--exec-only", action="store_true", help="Only execute remote command"
    )
    args = parser.parse_args()

    cfg = load_remote_config(args.config)

    with RemoteRunner(cfg) as runner:
        if args.upload_only:
            runner.upload()
        elif args.download_only:
            runner.download(args.download_dir)
        elif args.exec_only:
            code = runner.run_command()
            raise SystemExit(code)
        else:
            code = runner.run_full_cycle(args.download_dir)
            raise SystemExit(code)


if __name__ == "__main__":
    main()
