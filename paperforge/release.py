from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import zipfile
from collections.abc import Iterable, Mapping
from contextlib import closing
from pathlib import Path, PurePosixPath
from typing import IO, Any

from engine.secret_redaction import contains_secret

from .artifacts import sha256_file
from .models import CompletionGate
from .path_safety import atomic_write_text
from .publication.bundle import verify_source_lock
from .publication.compiler import PublicationCompiler
from .publication.diagnostics import DefaultLayoutDiagnostician
from .publication.engine import PUBLICATION_MANIFEST_SCHEMA, PublicationEngine
from .publication.invariants import InvariantSnapshot
from .publication.profiles import DEFAULT_TEMPLATE_REGISTRY
from .publication.renderer import PopplerRenderer
from .scientific_memory import ScientificMemory


class ReleaseVerificationError(RuntimeError):
    pass


_SECRET_PATTERNS = (
    re.compile(rb"sk-[A-Za-z0-9._-]{20,}"),
    re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(
        rb"(?i)\b(?:api[_-]?key|access[_-]?token|client[_-]?secret)\b"
        rb"\s*[:=]\s*[\"']?[A-Za-z0-9_./+=-]{20,}"
    ),
)
_TEXT_SUFFIXES = {
    ".cfg",
    ".conf",
    ".env",
    ".ini",
    ".json",
    ".jsonl",
    ".log",
    ".md",
    ".py",
    ".sh",
    ".tex",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
_IGNORED_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "node_modules",
}
_MAX_ARCHIVE_MEMBERS = 4096
_MAX_ARCHIVE_MEMBER_BYTES = 128 * 1024 * 1024
_MAX_ARCHIVE_TOTAL_BYTES = 512 * 1024 * 1024
_MAX_ARCHIVE_RATIO = 1000
_MAX_SECRET_SCAN_BYTES = 256 * 1024 * 1024
_SCAN_CHUNK_BYTES = 1024 * 1024
_SECRET_SCAN_OVERLAP = 4096


def _safe_manifest_path(
    workspace: Path,
    value: object,
    *,
    relative_to: Path | None = None,
) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ReleaseVerificationError("publication manifest contains an invalid path")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = (relative_to or workspace) / candidate
    lexical = Path(os.path.abspath(candidate))
    try:
        relative = lexical.relative_to(workspace)
    except ValueError as exc:
        raise ReleaseVerificationError(
            "publication artifact path leaves workspace"
        ) from exc
    current = workspace
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ReleaseVerificationError(
                "publication artifact path contains a symbolic link"
            )
    resolved = lexical.resolve()
    if resolved == workspace or workspace not in resolved.parents:
        raise ReleaseVerificationError("publication artifact path leaves workspace")
    if not resolved.is_file():
        raise ReleaseVerificationError(f"publication artifact is missing: {value}")
    return resolved


def _safe_workspace_directory(workspace: Path, value: object) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ReleaseVerificationError(
            "publication manifest contains an invalid directory"
        )
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    lexical = Path(os.path.abspath(candidate))
    try:
        relative = lexical.relative_to(workspace)
    except ValueError as exc:
        raise ReleaseVerificationError(
            "publication project root leaves release workspace"
        ) from exc
    current = workspace
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ReleaseVerificationError(
                "publication project root contains a symbolic link"
            )
    resolved = lexical.resolve()
    if (
        resolved != workspace
        and workspace not in resolved.parents
    ) or not resolved.is_dir():
        raise ReleaseVerificationError(
            "publication project root is missing or unsafe"
        )
    return resolved


def _secret_hits(content: bytes) -> bool:
    return any(pattern.search(content) for pattern in _SECRET_PATTERNS)


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items()}


def _stable_mapping_sha256(value: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _safe_archive_member(name: str) -> Path:
    if "\\" in name:
        raise ReleaseVerificationError("source bundle contains an unsafe path")
    posix = PurePosixPath(name)
    if posix.is_absolute() or not posix.parts or ".." in posix.parts:
        raise ReleaseVerificationError("source bundle contains an unsafe path")
    return Path(*posix.parts)


def _extract_source_bundle(bundle: Path, destination: Path) -> None:
    try:
        with zipfile.ZipFile(bundle) as archive:
            members = archive.infolist()
            if not members:
                raise ReleaseVerificationError("source bundle is empty")
            if len(members) > _MAX_ARCHIVE_MEMBERS:
                raise ReleaseVerificationError(
                    "source bundle contains too many members"
                )
            total_size = 0
            names: set[str] = set()
            for member in members:
                relative = _safe_archive_member(member.filename)
                canonical_name = relative.as_posix().rstrip("/").casefold()
                if not canonical_name or canonical_name in names:
                    raise ReleaseVerificationError(
                        "source bundle contains duplicate paths"
                    )
                names.add(canonical_name)
                if member.flag_bits & 0x1:
                    raise ReleaseVerificationError(
                        "source bundle contains an encrypted member"
                    )
                if member.file_size < 0 or member.file_size > _MAX_ARCHIVE_MEMBER_BYTES:
                    raise ReleaseVerificationError(
                        "source bundle member exceeds the size limit"
                    )
                total_size += member.file_size
                if total_size > _MAX_ARCHIVE_TOTAL_BYTES:
                    raise ReleaseVerificationError(
                        "source bundle exceeds the total size limit"
                    )
                if (
                    member.file_size
                    and (
                        member.compress_size <= 0
                        or member.file_size
                        > member.compress_size * _MAX_ARCHIVE_RATIO
                    )
                ):
                    raise ReleaseVerificationError(
                        "source bundle member exceeds the compression-ratio limit"
                    )
                mode = (member.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode):
                    raise ReleaseVerificationError(
                        "source bundle contains a symbolic link"
                    )
                file_type = stat.S_IFMT(mode)
                if (
                    file_type
                    and not member.is_dir()
                    and file_type != stat.S_IFREG
                ):
                    raise ReleaseVerificationError(
                        "source bundle contains a non-regular file"
                    )
                target = (destination / relative).resolve()
                if target != destination and destination not in target.parents:
                    raise ReleaseVerificationError(
                        "source bundle extraction leaves destination"
                    )
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                written = 0
                with archive.open(member) as source, target.open("wb") as output:
                    for chunk in iter(lambda: source.read(_SCAN_CHUNK_BYTES), b""):
                        written += len(chunk)
                        if written > member.file_size:
                            raise ReleaseVerificationError(
                                "source bundle member expanded beyond its declared size"
                            )
                        output.write(chunk)
                if written != member.file_size:
                    raise ReleaseVerificationError(
                        "source bundle member size does not match its declaration"
                    )
    except ReleaseVerificationError:
        raise
    except (
        OSError,
        RuntimeError,
        NotImplementedError,
        zipfile.BadZipFile,
    ) as exc:
        raise ReleaseVerificationError("source bundle is not a valid ZIP") from exc


def _iter_scannable_files(workspace: Path) -> Iterable[Path]:
    for path in sorted(workspace.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(workspace)
        if any(
            part in _IGNORED_PARTS or part.startswith(".venv")
            for part in relative.parts
        ):
            continue
        yield path


def _stream_has_secret(handle: IO[bytes], *, maximum_bytes: int) -> bool:
    scanned = 0
    overlap = b""
    while True:
        chunk = handle.read(_SCAN_CHUNK_BYTES)
        if not chunk:
            return False
        scanned += len(chunk)
        if scanned > maximum_bytes:
            raise ReleaseVerificationError("secret scan input exceeds the size limit")
        combined = overlap + chunk
        if _secret_hits(combined):
            return True
        overlap = combined[-_SECRET_SCAN_OVERLAP:]


def _scan_git_history(root: Path) -> tuple[int, list[dict[str, str]]]:
    if not (root / ".git").exists():
        return 0, []
    git = shutil.which("git")
    if git is None:
        return 0, [{"path": ".git", "reason": "Git is unavailable"}]
    try:
        listed = subprocess.run(
            (git, "-C", str(root), "rev-list", "--objects", "--all"),
            check=True,
            capture_output=True,
            text=True,
        )
        object_ids = [
            line.split(" ", 1)[0]
            for line in listed.stdout.splitlines()
            if line.strip()
        ]
        checked = subprocess.run(
            (
                git,
                "-C",
                str(root),
                "cat-file",
                "--batch-check=%(objectname) %(objecttype) %(objectsize)",
            ),
            input="\n".join(object_ids) + "\n",
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return 0, [{"path": ".git", "reason": "Git history scan failed"}]

    findings: list[dict[str, str]] = []
    blobs: list[tuple[str, int]] = []
    for line in checked.stdout.splitlines():
        parts = line.split()
        if len(parts) != 3 or parts[1] != "blob":
            continue
        try:
            size = int(parts[2])
        except ValueError:
            findings.append(
                {"path": f"git:{parts[0]}", "reason": "invalid Git blob metadata"}
            )
            continue
        blobs.append((parts[0], size))

    scanned = 0
    for object_id, size in blobs:
        scanned += 1
        if size > _MAX_SECRET_SCAN_BYTES:
            findings.append(
                {
                    "path": f"git:{object_id}",
                    "reason": "Git blob exceeds secret scan limit",
                }
            )
            continue
        try:
            process = subprocess.Popen(
                (git, "-C", str(root), "cat-file", "blob", object_id),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            if process.stdout is None:
                raise OSError("Git blob stream is unavailable")
            with closing(process.stdout):
                hit = _stream_has_secret(
                    process.stdout,
                    maximum_bytes=_MAX_SECRET_SCAN_BYTES,
                )
            returncode = process.wait()
            if returncode != 0:
                findings.append(
                    {"path": f"git:{object_id}", "reason": "Git blob scan failed"}
                )
            elif hit:
                findings.append(
                    {"path": f"git:{object_id}", "reason": "secret pattern"}
                )
        except (OSError, ReleaseVerificationError):
            findings.append(
                {"path": f"git:{object_id}", "reason": "Git blob scan failed"}
            )
    return scanned, findings


def scan_workspace_secrets(workspace: str | Path) -> dict[str, Any]:
    root = Path(workspace).expanduser().resolve()
    findings: list[dict[str, str]] = []
    scanned = 0
    for path in _iter_scannable_files(root):
        scanned += 1
        relative = path.relative_to(root).as_posix()
        try:
            size = path.stat().st_size
        except OSError:
            findings.append({"path": relative, "reason": "unreadable file"})
            continue
        if size > _MAX_SECRET_SCAN_BYTES:
            findings.append(
                {"path": relative, "reason": "file exceeds secret scan limit"}
            )
            continue
        if path.suffix.lower() != ".zip":
            try:
                with path.open("rb") as handle:
                    hit = _stream_has_secret(
                        handle,
                        maximum_bytes=_MAX_SECRET_SCAN_BYTES,
                    )
            except (OSError, ReleaseVerificationError):
                findings.append({"path": relative, "reason": "unreadable file"})
                continue
            if hit:
                findings.append({"path": relative, "reason": "secret pattern"})
            continue
        try:
            with zipfile.ZipFile(path) as archive:
                members = archive.infolist()
                if len(members) > _MAX_ARCHIVE_MEMBERS:
                    raise ReleaseVerificationError(
                        "ZIP contains too many members"
                    )
                total = sum(member.file_size for member in members)
                if total > _MAX_ARCHIVE_TOTAL_BYTES:
                    raise ReleaseVerificationError(
                        "ZIP exceeds the total size limit"
                    )
                for member in members:
                    if member.is_dir():
                        continue
                    if member.file_size > _MAX_SECRET_SCAN_BYTES:
                        findings.append(
                            {
                                "path": f"{relative}!/{member.filename}",
                                "reason": "ZIP member exceeds secret scan limit",
                            }
                        )
                        continue
                    if (
                        member.file_size
                        and (
                            member.compress_size <= 0
                            or member.file_size
                            > member.compress_size * _MAX_ARCHIVE_RATIO
                        )
                    ):
                        findings.append(
                            {
                                "path": f"{relative}!/{member.filename}",
                                "reason": "ZIP member exceeds compression-ratio limit",
                            }
                        )
                        continue
                    with archive.open(member) as handle:
                        hit = _stream_has_secret(
                            handle,
                            maximum_bytes=_MAX_SECRET_SCAN_BYTES,
                        )
                    if hit:
                        findings.append(
                            {
                                "path": f"{relative}!/{member.filename}",
                                "reason": "secret pattern",
                            }
                        )
        except (
            OSError,
            RuntimeError,
            NotImplementedError,
            ReleaseVerificationError,
            zipfile.BadZipFile,
        ):
            findings.append({"path": relative, "reason": "unreadable ZIP"})
    scanned_git_blobs, git_findings = _scan_git_history(root)
    findings.extend(git_findings)
    return {
        "clean": not findings,
        "scanned_files": scanned,
        "scanned_git_blobs": scanned_git_blobs,
        "findings": findings,
    }


def write_page_inspection(
    workspace: str | Path,
    *,
    pdf_path: str | Path,
    rendered_pages: Iterable[str | Path],
    reviewer: str,
    inspection_kind: str,
    render_integrity: Mapping[str, Any] | None = None,
    structural_review: Mapping[str, Any] | None = None,
    review_evidence: Mapping[str, Any] | None = None,
) -> Path:
    raw_pages = tuple(rendered_pages)
    if contains_secret(
        {
            "pdf_path": str(pdf_path),
            "rendered_pages": [str(path) for path in raw_pages],
            "reviewer": reviewer,
            "inspection_kind": inspection_kind,
            "render_integrity": dict(render_integrity or {}),
            "structural_review": dict(structural_review or {}),
            "review_evidence": dict(review_evidence or {}),
        }
    ):
        raise ReleaseVerificationError(
            "page inspection inputs must not contain credentials"
        )
    root = Path(workspace).expanduser().resolve()
    pdf = Path(pdf_path).expanduser().resolve()
    pages = tuple(Path(path).expanduser().resolve() for path in raw_pages)
    if not reviewer.strip() or not pdf.is_file() or not pages:
        raise ReleaseVerificationError("page inspection inputs are incomplete")
    normalized_kind = str(inspection_kind).strip().lower()
    if normalized_kind not in {"human", "automated-structural"}:
        raise ReleaseVerificationError("page inspection kind is unsupported")
    integrity_records: list[Mapping[str, Any]] = []
    structural_records: list[Mapping[str, Any]] = []
    if normalized_kind == "automated-structural":
        if render_integrity is None or structural_review is None:
            raise ReleaseVerificationError(
                "automated page inspection requires render integrity and structural review"
            )
        raw_records = render_integrity.get("pages")
        if (
            render_integrity.get("schema") != "paperforge.render-integrity/v1"
            or render_integrity.get("passed") is not True
            or not isinstance(raw_records, list)
            or len(raw_records) != len(pages)
        ):
            raise ReleaseVerificationError(
                "basic render integrity did not pass"
            )
        integrity_records = [_mapping(record) for record in raw_records]
        for page, record in zip(pages, integrity_records, strict=True):
            if (
                record.get("result") != "passed"
                or record.get("sha256") != sha256_file(page)
                or Path(str(record.get("path", ""))).expanduser().resolve()
                != page
            ):
                raise ReleaseVerificationError(
                    "basic render integrity is not bound to rendered pages"
                )
        raw_structural_records = structural_review.get("pages")
        if (
            structural_review.get("schema")
            != "paperforge.structural-page-review/v1"
            or structural_review.get("passed") is not True
            or not isinstance(raw_structural_records, list)
            or len(raw_structural_records) != len(pages)
        ):
            raise ReleaseVerificationError("structural page review did not pass")
        structural_records = [
            _mapping(record) for record in raw_structural_records
        ]
        for index, (page, record) in enumerate(
            zip(pages, structural_records, strict=True),
            start=1,
        ):
            if (
                record.get("page") != index
                or record.get("result") != "passed"
                or record.get("sha256") != sha256_file(page)
                or record.get("render_integrity_bound") is not True
                or record.get("layout_overlap_clean") is not True
                or int(record.get("text_characters") or 0) < 20
            ):
                raise ReleaseVerificationError(
                    "structural review is not bound to rendered pages"
                )
    else:
        evidence = _mapping(review_evidence)
        reviewed_pages = evidence.get("pages_reviewed")
        checks = evidence.get("checks")
        if (
            not isinstance(reviewed_pages, list)
            or reviewed_pages != list(range(1, len(pages) + 1))
            or not isinstance(checks, list)
            or not all(str(check).strip() for check in checks)
        ):
            raise ReleaseVerificationError(
                "human page inspection requires explicit page and check evidence"
            )
    payload = {
        "schema": "paperforge.page-inspection/v2",
        "status": "passed",
        "reviewer": reviewer.strip(),
        "inspection_kind": normalized_kind,
        "pdf": {
            "path": pdf.relative_to(root).as_posix(),
            "sha256": sha256_file(pdf),
            "page_count": len(pages),
        },
        "method": (
            str(structural_review.get("method"))
            if structural_review is not None
            else "explicit-human-review"
        ),
        "render_integrity": (
            dict(render_integrity) if render_integrity is not None else None
        ),
        "structural_review": (
            dict(structural_review) if structural_review is not None else None
        ),
        "review_evidence": (
            dict(review_evidence) if review_evidence is not None else None
        ),
        "pages": [
            {
                "page": index,
                "path": page.relative_to(root).as_posix(),
                "sha256": sha256_file(page),
                "result": "clean",
                **(
                    {
                        key: value
                        for key, value in integrity_records[index - 1].items()
                        if key not in {"path", "sha256", "result"}
                    }
                    if integrity_records
                    else {}
                ),
            }
            for index, page in enumerate(pages, start=1)
        ],
    }
    output = atomic_write_text(
        root,
        "artifacts/page-inspection.json",
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    return output


def _verify_page_inspection(
    workspace: Path,
    *,
    pdf_path: Path | None,
    rendered_pages: Iterable[Path],
) -> bool:
    try:
        path = _safe_manifest_path(
            workspace,
            "artifacts/page-inspection.json",
        )
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ReleaseVerificationError):
        return False
    if not isinstance(payload, Mapping):
        return False
    pdf = _mapping(payload.get("pdf"))
    page_records = payload.get("pages")
    pages = tuple(rendered_pages)
    if (
        payload.get("schema") != "paperforge.page-inspection/v2"
        or payload.get("status") != "passed"
        or not str(payload.get("reviewer", "")).strip()
        or payload.get("inspection_kind") not in {
            "human",
            "automated-structural",
        }
        or pdf_path is None
        or pdf.get("sha256") != sha256_file(pdf_path)
        or pdf.get("page_count") != len(pages)
        or not isinstance(page_records, list)
        or len(page_records) != len(pages)
    ):
        return False
    expected_hashes = [sha256_file(page) for page in pages]
    actual_hashes = []
    for record in page_records:
        normalized = _mapping(record)
        if normalized.get("result") != "clean":
            return False
        actual_hashes.append(normalized.get("sha256"))
    if actual_hashes != expected_hashes:
        return False
    if payload.get("inspection_kind") == "human":
        evidence = _mapping(payload.get("review_evidence"))
        return (
            evidence.get("pages_reviewed")
            == list(range(1, len(pages) + 1))
            and isinstance(evidence.get("checks"), list)
            and bool(evidence["checks"])
        )
    integrity = _mapping(payload.get("render_integrity"))
    structural = _mapping(payload.get("structural_review"))
    integrity_pages = integrity.get("pages")
    structural_pages = structural.get("pages")
    if (
        integrity.get("schema") != "paperforge.render-integrity/v1"
        or integrity.get("passed") is not True
        or structural.get("schema")
        != "paperforge.structural-page-review/v1"
        or structural.get("passed") is not True
        or not isinstance(integrity_pages, list)
        or not isinstance(structural_pages, list)
        or len(integrity_pages) != len(pages)
        or len(structural_pages) != len(pages)
    ):
        return False
    for index, (page, raw_integrity, raw_structural) in enumerate(
        zip(pages, integrity_pages, structural_pages, strict=True),
        start=1,
    ):
        page_hash = sha256_file(page)
        integrity_record = _mapping(raw_integrity)
        structural_record = _mapping(raw_structural)
        if (
            integrity_record.get("result") != "passed"
            or integrity_record.get("sha256") != page_hash
            or structural_record.get("page") != index
            or structural_record.get("result") != "passed"
            or structural_record.get("sha256") != page_hash
            or structural_record.get("render_integrity_bound") is not True
            or structural_record.get("layout_overlap_clean") is not True
            or int(structural_record.get("text_characters") or 0) < 20
        ):
            return False
    from .publication.visual_checks import (
        inspect_page_structure,
        inspect_rendered_pages,
    )

    fresh_integrity = inspect_rendered_pages(pages)
    expected_text_by_page = {
        index: tuple(
            str(value)
            for value in _mapping(record).get("expected_text") or ()
        )
        for index, record in enumerate(structural_pages, start=1)
    }
    fresh_structural = inspect_page_structure(
        pdf_path,
        pages,
        render_integrity=fresh_integrity,
        expected_text_by_page=expected_text_by_page,
    )
    if (
        fresh_integrity.get("passed") is not True
        or fresh_structural.get("passed") is not True
    ):
        return False
    fresh_records = fresh_structural.get("pages")
    if not isinstance(fresh_records, list) or len(fresh_records) != len(pages):
        return False
    return all(
        _mapping(fresh).get("text_sha256")
        == _mapping(recorded).get("text_sha256")
        for fresh, recorded in zip(
            fresh_records,
            structural_pages,
            strict=True,
        )
    )


def _load_publication_manifest(workspace: Path) -> tuple[Path, dict[str, Any]]:
    raw_candidates = [
        path
        for path in workspace.rglob("publication.manifest.json")
        if not any(part in _IGNORED_PARTS for part in path.relative_to(workspace).parts)
    ]
    candidates = []
    for path in raw_candidates:
        relative = path.relative_to(workspace).as_posix()
        candidates.append(_safe_manifest_path(workspace, relative))
    if len(candidates) != 1:
        raise ReleaseVerificationError(
            "release requires exactly one publication.manifest.json"
        )
    path = candidates[0]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseVerificationError("publication manifest is unreadable") from exc
    if not isinstance(payload, dict):
        raise ReleaseVerificationError("publication manifest root must be an object")
    return path, payload


class ReleaseVerifier:
    def __init__(
        self,
        workspace: str | Path,
        *,
        memory: ScientificMemory | None = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.memory = memory or ScientificMemory(
            self.workspace / ".paperforge" / "paperforge.db",
            trusted_root=self.workspace,
        )

    def _source_invariants(
        self,
        manifest: Mapping[str, Any],
        *,
        project_root: Path,
    ) -> dict[str, Any]:
        expected = _mapping(manifest.get("source_invariants"))
        entrypoint_value = manifest.get("entrypoint")
        bibliography_value = manifest.get("bibliography")
        if (
            not expected
            or not isinstance(entrypoint_value, str)
            or not isinstance(bibliography_value, str)
            or bibliography_value != "references.bib"
        ):
            return {"passed": False, "reason": "source invariants are incomplete"}
        try:
            entrypoint = _safe_manifest_path(
                self.workspace,
                entrypoint_value,
                relative_to=project_root,
            )
            bibliography = _safe_manifest_path(
                self.workspace,
                bibliography_value,
                relative_to=project_root,
            )
        except ReleaseVerificationError as exc:
            return {"passed": False, "reason": str(exc)}
        if entrypoint.suffix.lower() != ".tex":
            return {"passed": False, "reason": "publication entrypoint is not TeX"}

        claim_manifest = dict(self.memory.claim_manifest({}))
        claim_manifest.pop("generated_at", None)
        coverage = PublicationEngine._claim_coverage(entrypoint, claim_manifest)
        claim_manifest["coverage"] = coverage
        manifest_claims = manifest.get("claim_manifest")
        if not isinstance(manifest_claims, Mapping):
            return {"passed": False, "reason": "claim manifest is missing"}

        spans: dict[str, dict[str, Any]] = {}
        for claim in claim_manifest.get("claims", ()):
            if not isinstance(claim, Mapping):
                continue
            claim_id = claim.get("claim_id")
            span = claim.get("tex_span")
            if isinstance(claim_id, str) and isinstance(span, Mapping):
                spans[claim_id] = dict(span)
        snapshot = InvariantSnapshot.capture(
            entrypoint.read_text(encoding="utf-8"),
            scientific_memory=self.memory,
            claim_spans=spans,
        )
        actual = {
            "entrypoint_sha256": sha256_file(entrypoint),
            "bibliography_sha256": sha256_file(bibliography),
            "claim_manifest_sha256": _stable_mapping_sha256(claim_manifest),
            **snapshot.fingerprint(),
        }
        expected_hashes_valid = all(
            isinstance(value, str)
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value.lower())
            for value in expected.values()
        )
        manifest_claims_match = _stable_mapping_sha256(
            dict(manifest_claims)
        ) == actual["claim_manifest_sha256"]
        return {
            "passed": (
                expected_hashes_valid
                and actual == expected
                and manifest_claims_match
                and bool(coverage.get("passed"))
            ),
            "coverage": coverage,
            "manifest_claims_match": manifest_claims_match,
            "hashes_match": actual == expected,
            "entrypoint": entrypoint.relative_to(self.workspace).as_posix(),
        }

    def _revalidate_publication(
        self,
        manifest: Mapping[str, Any],
        *,
        official_pdf: Path,
        source_bundle: Path,
        external_source_lock: Path,
        expected_source_lock_sha256: str,
        inspected_pages: tuple[Path, ...],
    ) -> dict[str, Any]:
        try:
            profile_name = str(manifest["profile"])
            entrypoint = str(manifest["entrypoint"])
            with tempfile.TemporaryDirectory(
                prefix="paperforge-release-revalidation-"
            ) as temporary:
                root = Path(temporary).resolve()
                source = root / "source"
                source.mkdir()
                _extract_source_bundle(source_bundle, source)

                internal_lock = source / "publication.source.lock.json"
                internal_lock_sha256 = sha256_file(internal_lock)
                external_lock_sha256 = sha256_file(external_source_lock)
                lock_bytes_match = (
                    internal_lock.read_bytes()
                    == external_source_lock.read_bytes()
                )
                locks_bound = (
                    internal_lock_sha256 == expected_source_lock_sha256
                    and external_lock_sha256 == expected_source_lock_sha256
                    and lock_bytes_match
                )
                if not locks_bound:
                    return {
                        "passed": False,
                        "reason": (
                            "source bundle lock is not bound to the "
                            "authoritative source lock"
                        ),
                        "bundle_source_lock_matches_external": False,
                    }
                lock_verification = verify_source_lock(source, internal_lock)
                if not lock_verification.valid:
                    return {
                        "passed": False,
                        "reason": "source bundle internal lock failed",
                    }

                tex_path = (source / entrypoint).resolve()
                if (
                    tex_path != source
                    and source not in tex_path.parents
                ) or not tex_path.is_file():
                    return {
                        "passed": False,
                        "reason": "source bundle entrypoint is missing or unsafe",
                    }
                tex_text = tex_path.read_text(encoding="utf-8")
                profile = DEFAULT_TEMPLATE_REGISTRY.resolve(
                    profile_name,
                    tex_text=tex_text,
                )
                detected = DEFAULT_TEMPLATE_REGISTRY.detect(tex_text)
                if detected.name != profile.name:
                    return {
                        "passed": False,
                        "reason": (
                            f"template profile mismatch: expected {profile.name}, "
                            f"detected {detected.name}"
                        ),
                    }

                compiler = PublicationCompiler()
                compile_result = compiler.compile(
                    source,
                    main_tex=entrypoint,
                    output_pdf="release-revalidated.pdf",
                )
                render_result = None
                if compile_result.success and compile_result.pdf_path is not None:
                    render_result = PopplerRenderer(compiler.toolchain).render(
                        compile_result.pdf_path,
                        root / "rebuilt-pages",
                    )
                diagnosis = DefaultLayoutDiagnostician().diagnose(
                    compile_result,
                    render_result,
                    profile,
                )

                official_render = PopplerRenderer(compiler.toolchain).render(
                    official_pdf,
                    root / "official-pages",
                )
                inspected_hashes = tuple(
                    sha256_file(page) for page in inspected_pages
                )
                official_hashes = tuple(
                    sha256_file(page) for page in official_render.pages
                )
                official_pages_match_inspection = (
                    official_render.success
                    and inspected_hashes == official_hashes
                )
                rebuilt_hashes = (
                    tuple(sha256_file(page) for page in render_result.pages)
                    if render_result is not None and render_result.success
                    else ()
                )
                rebuilt_pages_match_official = (
                    bool(rebuilt_hashes)
                    and rebuilt_hashes == official_hashes
                )
                passed = bool(
                    compile_result.success
                    and render_result is not None
                    and render_result.success
                    and diagnosis.clean
                    and official_pages_match_inspection
                    and rebuilt_pages_match_official
                )
                return {
                    "passed": passed,
                    "bundle_source_lock_matches_external": locks_bound,
                    "compile_clean": bool(compile_result.success),
                    "render_clean": bool(
                        render_result is not None and render_result.success
                    ),
                    "diagnostics_clean": bool(diagnosis.clean),
                    "official_pages_match_inspection": (
                        official_pages_match_inspection
                    ),
                    "rebuilt_pages_match_official": (
                        rebuilt_pages_match_official
                    ),
                    "rebuilt_page_count": (
                        render_result.page_count
                        if render_result is not None
                        else 0
                    ),
                    "official_page_count": official_render.page_count,
                }
        except (
            KeyError,
            OSError,
            TypeError,
            ValueError,
            UnicodeError,
            ReleaseVerificationError,
        ) as exc:
            return {
                "passed": False,
                "reason": str(exc),
                "error_type": type(exc).__name__,
            }

    def verify(self) -> CompletionGate:
        details: dict[str, Any] = {}
        claim_gate = self.memory.claim_gate(final_publication=True)
        details["claim_gate"] = claim_gate

        try:
            manifest_path, manifest = _load_publication_manifest(self.workspace)
            manifest_valid = (
                manifest.get("schema") == PUBLICATION_MANIFEST_SCHEMA
                and manifest.get("status") == "passed"
            )
        except ReleaseVerificationError as exc:
            manifest_path = None
            manifest = {}
            manifest_valid = False
            details["manifest_error"] = str(exc)

        artifacts = _mapping(manifest.get("artifacts"))
        project_root = _safe_workspace_directory(
            self.workspace,
            manifest.get("project_root", "."),
        )
        pdf_record = _mapping(artifacts.get("pdf"))
        pdf_valid = False
        try:
            pdf_path = _safe_manifest_path(
                self.workspace,
                pdf_record.get("path"),
            )
            pdf_valid = (
                pdf_path.read_bytes()[:4] == b"%PDF"
                and sha256_file(pdf_path) == pdf_record.get("sha256")
            )
        except (OSError, ReleaseVerificationError):
            pdf_path = None

        raw_rounds = manifest.get("rounds")
        rounds: list[Any] = raw_rounds if isinstance(raw_rounds, list) else []
        final_round = _mapping(rounds[-1]) if rounds else {}
        render = _mapping(final_round.get("render"))
        raw_pages = render.get("rendered_pages")
        rendered_pages: list[Any] = raw_pages if isinstance(raw_pages, list) else []
        pages_valid = bool(render.get("success")) and bool(rendered_pages)
        resolved_pages: tuple[Path, ...] = ()
        if pages_valid:
            try:
                resolved_pages = tuple(
                    _safe_manifest_path(self.workspace, value)
                    for value in rendered_pages
                )
                pages_valid = all(
                    page.suffix.lower() == ".png" for page in resolved_pages
                )
            except ReleaseVerificationError:
                pages_valid = False
        inspection_valid = pages_valid and _verify_page_inspection(
            self.workspace,
            pdf_path=pdf_path,
            rendered_pages=resolved_pages,
        )

        bundle_record = _mapping(artifacts.get("source_bundle"))
        source_lock_valid = False
        source_lock_hash_valid = False
        bundle_valid = False
        checksum_valid = False
        bundle_path: Path | None = None
        source_lock_path: Path | None = None
        expected_source_lock_sha256 = ""
        try:
            bundle_path = _safe_manifest_path(
                self.workspace, bundle_record.get("path")
            )
            bundle_valid = sha256_file(bundle_path) == bundle_record.get("sha256")
            source_lock_path = _safe_manifest_path(
                self.workspace, bundle_record.get("source_lock_path")
            )
            expected_source_lock_sha256 = str(
                bundle_record.get("source_lock_sha256") or ""
            )
            source_lock_hash_valid = (
                sha256_file(source_lock_path)
                == expected_source_lock_sha256
            )
            source_lock_valid = verify_source_lock(
                project_root, source_lock_path
            ).valid
            checksum_path = _safe_manifest_path(
                self.workspace, bundle_record.get("checksum_path")
            )
            checksum_valid = (
                checksum_path.read_text(encoding="ascii")
                == f"{sha256_file(bundle_path)}  {bundle_path.name}\n"
            )
        except (OSError, ReleaseVerificationError):
            pass

        source_invariants = self._source_invariants(
            manifest,
            project_root=project_root,
        )
        if (
            pdf_path is not None
            and bundle_path is not None
            and source_lock_path is not None
            and source_lock_hash_valid
            and resolved_pages
        ):
            revalidation = self._revalidate_publication(
                manifest,
                official_pdf=pdf_path,
                source_bundle=bundle_path,
                external_source_lock=source_lock_path,
                expected_source_lock_sha256=expected_source_lock_sha256,
                inspected_pages=resolved_pages,
            )
        else:
            revalidation = {
                "passed": False,
                "reason": "publication artifacts are incomplete",
            }

        secret_scan = scan_workspace_secrets(self.workspace)
        details.update(
            {
                "publication_manifest": (
                    str(manifest_path.relative_to(self.workspace))
                    if manifest_path is not None
                    else None
                ),
                "pdf": str(pdf_path.relative_to(self.workspace)) if pdf_path else None,
                "secret_scan": secret_scan,
                "page_inspection_verified": inspection_valid,
                "source_lock_verified": source_lock_valid,
                "source_lock_hash_verified": source_lock_hash_valid,
                "bundle_verified": bundle_valid,
                "bundle_checksum_verified": checksum_valid,
                "source_invariants": source_invariants,
                "publication_revalidation": revalidation,
            }
        )
        completion = CompletionGate(
            claim_gate_passed=bool(claim_gate.get("passed"))
            and bool(
                _mapping(source_invariants.get("coverage")).get("passed")
            ),
            required_artifacts_present=pdf_valid and bundle_valid,
            latex_clean_compile=bool(revalidation.get("passed")),
            all_pdf_pages_inspected=inspection_valid
            and bool(revalidation.get("official_pages_match_inspection")),
            protected_hashes_unchanged=bool(source_invariants.get("passed")),
            secret_scan_clean=bool(secret_scan["clean"]),
            release_manifest_verified=(
                manifest_valid
                and source_lock_valid
                and source_lock_hash_valid
                and checksum_valid
                and bool(source_invariants.get("passed"))
                and bool(revalidation.get("passed"))
                and bool(
                    revalidation.get(
                        "bundle_source_lock_matches_external"
                    )
                )
                and bool(
                    revalidation.get("rebuilt_pages_match_official")
                )
            ),
            details=details,
        )
        return completion

    def write_report(self, gate: CompletionGate) -> Path:
        payload = {
            "schema": "paperforge.release.manifest/v1",
            "gate": gate.to_dict(),
        }
        if contains_secret(payload):
            raise ReleaseVerificationError(
                "release report must not contain credentials"
            )
        canonical = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        payload["sha256"] = hashlib.sha256(canonical).hexdigest()
        return atomic_write_text(
            self.workspace,
            ".paperforge/release_manifest.json",
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )


__all__ = [
    "ReleaseVerificationError",
    "ReleaseVerifier",
    "scan_workspace_secrets",
    "write_page_inspection",
]
