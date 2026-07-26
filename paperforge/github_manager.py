from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from engine.secret_redaction import contains_secret, redact_command, redact_secrets

from .models import utc_now
from .path_safety import (
    is_link_or_reparse_point,
    reject_symlink_components,
    safe_mkdir,
    validate_writable_path,
)


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

    def _run_gh(self, args: Sequence[str]) -> GitResult:
        executable = shutil.which("gh")
        if executable is None:
            raise GitManagerError("GitHub CLI is required for approved remote actions")
        command = (executable, *tuple(str(arg) for arg in args))
        completed = subprocess.run(
            command,
            cwd=self.repository,
            text=True,
            capture_output=True,
            check=False,
            env={
                **os.environ,
                "GH_PROMPT_DISABLED": "1",
                "GIT_TERMINAL_PROMPT": "0",
            },
        )
        result = GitResult(
            command=tuple(redact_command(("gh", *tuple(str(arg) for arg in args)))),
            returncode=completed.returncode,
            stdout=redact_secrets(completed.stdout),
            stderr=redact_secrets(completed.stderr),
        )
        if not result.success:
            detail = result.stderr.strip().splitlines()[-1:] or result.stdout.strip().splitlines()[-1:]
            raise GitManagerError(detail[0] if detail else "GitHub CLI command failed")
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
        if parsed.username or parsed.password or contains_secret(url):
            raise GitManagerError("remote URL must not contain credentials")
        if parsed.query or parsed.fragment:
            raise GitManagerError("remote URL must not contain a query or fragment")
        if parsed.scheme and parsed.scheme not in {"https", "ssh", "file"}:
            raise GitManagerError("unsupported remote URL scheme")

    def _approved_remote_url(self, remote: str, *, approved: bool) -> str:
        if not self.allow_remote or not approved:
            raise GitApprovalRequired("remote Git actions require explicit approval")
        remote_url = self._run(("remote", "get-url", remote)).stdout.strip()
        self._validate_remote_url(remote_url)
        return remote_url

    @staticmethod
    def _gh_repository_selector(remote_url: str) -> str:
        """Convert a verified Git remote URL to GH's explicit HOST/OWNER/REPO form."""

        parsed = urlsplit(remote_url)
        if parsed.scheme in {"https", "ssh"}:
            host = parsed.hostname or ""
            path = parsed.path
        elif "://" not in remote_url:
            match = re.fullmatch(r"(?:[^@/\s]+@)?([^:/\s]+):(.+)", remote_url)
            host = match.group(1) if match else ""
            path = match.group(2) if match else ""
        else:
            host = ""
            path = ""
        parts = [part for part in path.strip("/").split("/") if part]
        if len(parts) != 2 or not host:
            raise GitManagerError(
                "approved GitHub remote must identify HOST/OWNER/REPO"
            )
        owner, repository = parts
        if repository.endswith(".git"):
            repository = repository[:-4]
        if not owner or not repository:
            raise GitManagerError(
                "approved GitHub remote must identify HOST/OWNER/REPO"
            )
        return f"{host}/{owner}/{repository}"

    def push(
        self,
        *,
        remote: str,
        refspec: str,
        approved: bool,
        set_upstream: bool = False,
    ) -> GitResult:
        self._approved_remote_url(remote, approved=approved)
        args = ["push"]
        if set_upstream:
            args.append("--set-upstream")
        args.extend((remote, refspec))
        return self._run(tuple(args))

    def create_pull_request(
        self,
        *,
        base: str,
        head: str,
        title: str,
        body: str,
        publish: bool = False,
        approved: bool = False,
        remote: str = "origin",
    ) -> Path:
        """Create a durable local PR record and optionally publish it remotely."""

        cleaned_title = str(title).strip()
        cleaned_body = str(body).strip()
        if not cleaned_title:
            raise ValueError("pull request title is required")
        if contains_secret({"title": cleaned_title, "body": cleaned_body}):
            raise GitManagerError("pull request metadata must not contain credentials")
        record = self.create_pr_record(
            base=base,
            head=head,
            title=cleaned_title,
            body=cleaned_body,
        )
        if not publish:
            return record
        remote_url = self._approved_remote_url(remote, approved=approved)
        repository = self._gh_repository_selector(remote_url)
        outcome = self._run_gh(
            (
                "pr",
                "create",
                "--base",
                base,
                "--head",
                head,
                "--title",
                cleaned_title,
                "--body",
                cleaned_body,
                "--repo",
                repository,
            )
        )
        payload = json.loads(record.read_text(encoding="utf-8"))
        payload.update(
            {
                "local_only": False,
                "remote": remote,
                "remote_url": outcome.stdout.strip(),
            }
        )
        record.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return record

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

    def publish_release(
        self,
        *,
        tag: str,
        artifacts: Sequence[str | Path],
        title: str | None = None,
        notes: str = "",
        publish: bool = False,
        approved: bool = False,
        remote: str = "origin",
    ) -> Path:
        """Record a release locally and publish only with explicit authorization."""

        cleaned_title = str(title or tag).strip()
        cleaned_notes = str(notes).strip()
        if not cleaned_title:
            raise ValueError("release title is required")
        if contains_secret({"title": cleaned_title, "notes": cleaned_notes}):
            raise GitManagerError("release metadata must not contain credentials")
        record = self.release_record(tag=tag, artifacts=artifacts)
        if not publish:
            return record
        remote_url = self._approved_remote_url(remote, approved=approved)
        repository = self._gh_repository_selector(remote_url)
        artifact_paths = tuple(
            str(Path(artifact).expanduser().resolve()) for artifact in artifacts
        )
        outcome = self._run_gh(
            (
                "release",
                "create",
                tag,
                *artifact_paths,
                "--title",
                cleaned_title,
                "--notes",
                cleaned_notes,
                "--repo",
                repository,
            )
        )
        payload = json.loads(record.read_text(encoding="utf-8"))
        payload.update(
            {
                "remote_published": True,
                "remote": remote,
                "remote_url": outcome.stdout.strip(),
            }
        )
        record.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return record

    def create_research_archive(
        self,
        destination: str | Path,
        *,
        paths: Sequence[str | Path],
        metadata: Mapping[str, Any] | None = None,
    ) -> Path:
        """Create a deterministic, secret-checked archive from an explicit allowlist."""

        if not paths:
            raise ValueError("research archive requires at least one allowlisted path")
        destination_path = validate_writable_path(destination)
        archive_metadata = dict(metadata or {})
        if contains_secret(archive_metadata):
            raise GitManagerError("research archive metadata must not contain credentials")
        selected: dict[str, tuple[bytes, bool]] = {}
        for raw in paths:
            relative = Path(raw)
            if (
                relative.is_absolute()
                or not relative.parts
                or any(part in {"", ".", "..", ".git"} for part in relative.parts)
            ):
                raise ValueError(f"unsafe archive path: {raw}")
            candidate = self.repository / relative
            reject_symlink_components(candidate, anchor=self.repository)
            if is_link_or_reparse_point(candidate) or not candidate.exists():
                raise FileNotFoundError(candidate)
            files = (candidate,) if candidate.is_file() else tuple(sorted(candidate.rglob("*")))
            for source in files:
                if source.is_dir():
                    continue
                same_destination = source.absolute() == destination_path
                if not same_destination and destination_path.exists():
                    try:
                        same_destination = os.path.samefile(source, destination_path)
                    except OSError:
                        same_destination = False
                if same_destination:
                    continue
                reject_symlink_components(source, anchor=self.repository)
                if is_link_or_reparse_point(source) or not source.is_file():
                    raise GitManagerError(f"archive source is not a regular file: {source}")
                archive_name = source.relative_to(self.repository).as_posix()
                if archive_name == "RESEARCH_ARCHIVE_MANIFEST.json":
                    raise GitManagerError("archive source collides with the manifest")
                payload = source.read_bytes()
                if contains_secret(payload):
                    raise GitManagerError(f"archive source contains credentials: {archive_name}")
                selected[archive_name] = (
                    payload,
                    bool(source.stat().st_mode & 0o111),
                )
        if not selected:
            raise ValueError("research archive allowlist contains no files")

        records = [
            {
                "path": name,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
            }
            for name, (payload, _) in sorted(selected.items())
        ]
        manifest = json.dumps(
            {
                "schema": "paperforge.research-archive/v1",
                "commit": self.head(allow_unborn=True),
                "files": records,
                "metadata": archive_metadata,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8") + b"\n"
        parent = safe_mkdir(destination_path.parent)
        destination_path = parent / destination_path.name
        reject_symlink_components(destination_path, anchor=parent)
        if is_link_or_reparse_point(destination_path) or (
            destination_path.exists() and not destination_path.is_file()
        ):
            raise GitManagerError("research archive destination is unsafe")
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=parent,
                prefix=f".{destination_path.name}.",
                delete=False,
            ) as stream:
                temporary = stream.name
            with zipfile.ZipFile(
                temporary,
                "w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            ) as archive:
                for name, (payload, executable) in sorted(selected.items()):
                    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = (0o755 if executable else 0o644) << 16
                    archive.writestr(info, payload)
                info = zipfile.ZipInfo(
                    "RESEARCH_ARCHIVE_MANIFEST.json",
                    date_time=(1980, 1, 1, 0, 0, 0),
                )
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                archive.writestr(info, manifest)
            os.replace(temporary, destination_path)
            temporary = None
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
        return destination_path
