from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from engine.secret_redaction import redact_secrets

from .models import utc_now


class GitManagerError(RuntimeError):
    pass


class GitApprovalRequired(GitManagerError, PermissionError):
    pass


@dataclass(frozen=True)
class GitResult:
    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def success(self) -> bool:
        return self.returncode == 0


class GitHubManager:
    """Git research manager that is local-only unless remote use is explicit."""

    def __init__(
        self,
        repository: str | Path,
        *,
        allow_remote: bool = False,
    ) -> None:
        self.repository = Path(repository).expanduser().resolve()
        self.repository.mkdir(parents=True, exist_ok=True)
        self.allow_remote = bool(allow_remote)

    def _run(
        self,
        args: Sequence[str],
        *,
        input_text: str | None = None,
        check: bool = True,
    ) -> GitResult:
        command = ("git", "-C", str(self.repository), *tuple(str(arg) for arg in args))
        completed = subprocess.run(
            command,
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
            env={
                **os.environ,
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_NOSYSTEM": "1",
            },
        )
        result = GitResult(
            command=("git", "-C", "[repository]", *tuple(str(arg) for arg in args)),
            returncode=completed.returncode,
            stdout=redact_secrets(completed.stdout),
            stderr=redact_secrets(completed.stderr),
        )
        if check and not result.success:
            detail = result.stderr.strip().splitlines()[-1:] or result.stdout.strip().splitlines()[-1:]
            raise GitManagerError(detail[0] if detail else "git command failed")
        return result

    def initialize(self, *, default_branch: str = "main") -> str:
        if not (self.repository / ".git").exists():
            self._run(("init", "-b", default_branch))
        return self.head(allow_unborn=True)

    def head(self, *, allow_unborn: bool = False) -> str:
        result = self._run(("rev-parse", "HEAD"), check=not allow_unborn)
        return result.stdout.strip() if result.success else "UNBORN"

    def create_branch(self, name: str, *, start_point: str = "HEAD") -> None:
        if not name or name.startswith("-") or any(character.isspace() for character in name):
            raise ValueError("invalid branch name")
        self._run(("check-ref-format", "--branch", name))
        self._run(("switch", "-c", name, start_point))

    def apply_patch(self, patch_file: str | Path) -> None:
        patch = Path(patch_file).expanduser().resolve()
        if not patch.is_file() or patch.is_symlink():
            raise FileNotFoundError(patch)
        content = patch.read_text(encoding="utf-8")
        self._run(("apply", "--check", "-"), input_text=content)
        self._run(("apply", "-"), input_text=content)

    def commit(
        self,
        message: str,
        *,
        paths: Sequence[str] = (".",),
        author_name: str = "PaperForge",
        author_email: str = "paperforge@localhost",
    ) -> str:
        cleaned = str(message).strip()
        if not cleaned or "\x00" in cleaned:
            raise ValueError("commit message is required")
        for path in paths:
            if Path(path).is_absolute() or ".." in Path(path).parts:
                raise ValueError(f"unsafe commit path: {path}")
        self._run(("add", "--", *paths))
        self._run(
            (
                "-c",
                f"user.name={author_name}",
                "-c",
                f"user.email={author_email}",
                "commit",
                "-m",
                cleaned,
            )
        )
        return self.head()

    def tag(self, name: str, *, message: str | None = None) -> str:
        if not name or name.startswith("-") or any(character.isspace() for character in name):
            raise ValueError("invalid tag name")
        self._run(("check-ref-format", f"refs/tags/{name}"))
        args = (
            "-c",
            "user.name=PaperForge",
            "-c",
            "user.email=paperforge@localhost",
            "tag",
            "-a",
            name,
            "-m",
            message or name,
        )
        self._run(args)
        return self._run(("rev-parse", f"refs/tags/{name}^{{}}")).stdout.strip()

    def write_citation(self, payload: Mapping[str, Any]) -> Path:
        required = {"title", "version", "date-released"}
        if not required.issubset(payload):
            raise ValueError(f"CITATION metadata requires: {sorted(required)}")
        citation = self.repository / "CITATION.cff"
        authors = payload.get("authors") or [{"name": "PaperForge contributors"}]
        lines = [
            "cff-version: 1.2.0",
            'message: "If you use this software, please cite it."',
            f"title: {json.dumps(str(payload['title']), ensure_ascii=False)}",
            f"version: {json.dumps(str(payload['version']))}",
            f"date-released: {json.dumps(str(payload['date-released']))}",
            "authors:",
        ]
        for author in authors:
            lines.append(f"  - name: {json.dumps(str(author.get('name', 'Unknown')), ensure_ascii=False)}")
        citation.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return citation

    def create_pr_record(
        self,
        *,
        base: str,
        head: str,
        title: str,
        body: str,
    ) -> Path:
        self._run(("rev-parse", "--verify", base))
        self._run(("rev-parse", "--verify", head))
        record_dir = self.repository / ".paperforge" / "pull-requests"
        record_dir.mkdir(parents=True, exist_ok=True)
        record = record_dir / f"{head.replace('/', '-')}-to-{base.replace('/', '-')}.json"
        payload = {
            "schema": "paperforge.local-pr/v1",
            "base": base,
            "head": head,
            "title": title,
            "body": body,
            "created_at": utc_now(),
            "local_only": True,
        }
        record.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return record

    @staticmethod
    def _validate_remote_url(url: str) -> None:
        parsed = urlsplit(url)
        if parsed.username or parsed.password:
            raise GitManagerError("remote URL must not contain credentials")
        if parsed.scheme and parsed.scheme not in {"https", "ssh", "file"}:
            raise GitManagerError("unsupported remote URL scheme")

    def push(
        self,
        *,
        remote: str,
        refspec: str,
        approved: bool,
        set_upstream: bool = False,
    ) -> GitResult:
        if not self.allow_remote or not approved:
            raise GitApprovalRequired("remote Git actions require explicit approval")
        remote_url = self._run(("remote", "get-url", remote)).stdout.strip()
        self._validate_remote_url(remote_url)
        args = ["push"]
        if set_upstream:
            args.append("--set-upstream")
        args.extend((remote, refspec))
        return self._run(tuple(args))

    def release_record(
        self,
        *,
        tag: str,
        artifacts: Sequence[str | Path],
    ) -> Path:
        tag_commit = self._run(("rev-parse", f"refs/tags/{tag}^{{}}")).stdout.strip()
        records = []
        for artifact in artifacts:
            path = Path(artifact).expanduser().resolve()
            if not path.is_file() or path.is_symlink():
                raise FileNotFoundError(path)
            import hashlib

            records.append(
                {
                    "name": path.name,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "size_bytes": path.stat().st_size,
                }
            )
        release_dir = self.repository / ".paperforge" / "releases"
        release_dir.mkdir(parents=True, exist_ok=True)
        path = release_dir / f"{tag}.json"
        path.write_text(
            json.dumps(
                {
                    "schema": "paperforge.git-release/v1",
                    "tag": tag,
                    "commit": tag_commit,
                    "artifacts": records,
                    "created_at": utc_now(),
                    "remote_published": False,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return path
