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
import yaml
from pathlib import Path
from typing import Any, Dict, List, Optional


def _lazy_import_paramiko():
    try:
        import paramiko
        return paramiko
    except ImportError:
        raise ImportError(
            "paramiko is required for remote execution. "
            "Install it with: pip install paramiko"
        )


def _resolve_env(value: Any) -> str:
    """Resolve $ENV_VAR references in string values."""
    if not isinstance(value, str):
        return str(value) if value is not None else ""
    if value.startswith("$"):
        return os.environ.get(value[1:], "")
    return value


def load_remote_config(config_path: str) -> Dict:
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    auth = cfg.get("auth", {})
    for key in ("password", "passphrase"):
        if key in auth:
            auth[key] = _resolve_env(auth[key])

    defaults = {
        "host": "",
        "port": 22,
        "username": "root",
        "auth": {"method": "key", "key_path": "~/.ssh/id_rsa"},
        "remote_workdir": "/root/experiment",
        "upload_paths": [],
        "upload_excludes": ["__pycache__", ".git", "*.pyc", ".DS_Store"],
        "train_command": "",
        "results_dir": "",
        "download_excludes": ["__pycache__", "*.pyc", ".git"],
        "poll_interval_seconds": 30,
        "connect_timeout": 15,
    }
    for k, v in defaults.items():
        cfg.setdefault(k, v)

    if not cfg["host"]:
        raise ValueError("remote config: 'host' is required")
    if not cfg["train_command"]:
        raise ValueError("remote config: 'train_command' is required")
    if not cfg["results_dir"]:
        raise ValueError("remote config: 'results_dir' is required")

    return cfg


class RemoteRunner:
    """Manages SSH connection, file transfer, and remote command execution."""

    def __init__(self, config: Dict):
        self.cfg = config
        self.paramiko = _lazy_import_paramiko()
        self._client: Any = None
        self._sftp: Any = None

    # ── Connection ──────────────────────────────────────────────

    def connect(self) -> None:
        print(f"[remote] connecting to {self.cfg['host']}:{self.cfg['port']} "
              f"as {self.cfg['username']} ...")
        client = self.paramiko.SSHClient()
        client.set_missing_host_key_policy(self.paramiko.AutoAddPolicy())

        auth = self.cfg.get("auth", {})
        method = auth.get("method", "key")
        kwargs: Dict[str, Any] = {
            "hostname": self.cfg["host"],
            "port": int(self.cfg["port"]),
            "username": self.cfg["username"],
            "timeout": self.cfg.get("connect_timeout", 15),
        }

        if method == "key":
            key_path = osp.expanduser(auth.get("key_path", "~/.ssh/id_rsa"))
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

    def _should_exclude(self, name: str, excludes: List[str]) -> bool:
        return any(fnmatch.fnmatch(name, pat) for pat in excludes)

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
            try:
                self._sftp.mkdir(d)
            except IOError:
                pass

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
            local_path = Path(local_path_str).resolve()
            if not local_path.exists():
                print(f"[remote][upload] skip missing: {local_path}")
                continue

            if local_path.is_file():
                remote_file = f"{remote_base}/{local_path.name}"
                print(f"  {local_path} -> {remote_file}")
                self._sftp.put(str(local_path), remote_file)
                count += 1
            elif local_path.is_dir():
                count += self._upload_dir(
                    str(local_path), remote_base, excludes
                )

        print(f"[remote][upload] {count} files uploaded")
        return count

    def _upload_dir(
        self, local_dir: str, remote_base: str, excludes: List[str]
    ) -> int:
        count = 0
        dir_name = osp.basename(local_dir)
        remote_root = f"{remote_base}/{dir_name}"
        self._sftp_mkdir_p(remote_root)

        for dirpath, dirnames, filenames in os.walk(local_dir):
            dirnames[:] = [
                d for d in dirnames if not self._should_exclude(d, excludes)
            ]
            rel = osp.relpath(dirpath, local_dir)
            remote_dir = (
                f"{remote_root}/{rel}" if rel != "." else remote_root
            )
            self._sftp_mkdir_p(remote_dir)

            for fname in filenames:
                if self._should_exclude(fname, excludes):
                    continue
                local_file = osp.join(dirpath, fname)
                remote_file = f"{remote_dir}/{fname}"
                print(f"  {local_file} -> {remote_file}")
                self._sftp.put(local_file, remote_file)
                count += 1

        return count

    # ── Remote Execution ────────────────────────────────────────

    def run_command(self, command: Optional[str] = None) -> int:
        """Execute a command on the remote server.

        Streams stdout/stderr in real time. Returns the exit code.
        """
        cmd = command or self.cfg["train_command"]
        print(f"[remote][exec] {cmd}")

        transport = self._client.get_transport()
        channel = transport.open_session()
        channel.set_combine_stderr(True)
        channel.exec_command(cmd)

        while True:
            if channel.recv_ready():
                data = channel.recv(4096)
                if data:
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()
            if channel.exit_status_ready():
                while channel.recv_ready():
                    data = channel.recv(4096)
                    if data:
                        sys.stdout.buffer.write(data)
                        sys.stdout.buffer.flush()
                break
            time.sleep(0.1)

        exit_code = channel.recv_exit_status()
        channel.close()
        print(f"\n[remote][exec] exit code: {exit_code}")
        return exit_code

    # ── Download ────────────────────────────────────────────────

    def download(self, local_dest: str) -> int:
        """Download results_dir from remote to local_dest. Returns file count."""
        remote_dir = self.cfg["results_dir"]
        excludes = self.cfg.get("download_excludes", [])
        local_dest_path = Path(local_dest)
        local_dest_path.mkdir(parents=True, exist_ok=True)

        print(f"[remote][download] {remote_dir} -> {local_dest}")
        count = self._download_dir(remote_dir, str(local_dest_path), excludes)
        print(f"[remote][download] {count} files downloaded")
        return count

    def _download_dir(
        self, remote_dir: str, local_dir: str, excludes: List[str]
    ) -> int:
        count = 0
        try:
            entries = self._sftp.listdir_attr(remote_dir)
        except FileNotFoundError:
            print(f"[remote][download] remote dir not found: {remote_dir}")
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
                print(f"  {remote_path} -> {local_path}")
                self._sftp.get(remote_path, local_path)
                count += 1

        return count

    # ── Full Cycle ──────────────────────────────────────────────

    def run_full_cycle(self, local_download_dir: str) -> int:
        """Upload -> execute -> download. Returns remote exit code."""
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
        "--upload-only", action="store_true", help="Only upload, skip exec and download"
    )
    parser.add_argument(
        "--download-only", action="store_true", help="Only download, skip upload and exec"
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
