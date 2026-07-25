from __future__ import annotations

import hashlib
import json
import re
import zipfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .artifacts import sha256_file
from .models import CompletionGate
from .publication.bundle import verify_source_lock
from .publication.engine import PUBLICATION_MANIFEST_SCHEMA
from .scientific_memory import ScientificMemory


class ReleaseVerificationError(RuntimeError):
    pass


_SECRET_PATTERNS = (
    re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}\b"),
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
    resolved = candidate.resolve()
    if resolved == workspace or workspace not in resolved.parents:
        raise ReleaseVerificationError("publication artifact path leaves workspace")
    if resolved.is_symlink() or not resolved.is_file():
        raise ReleaseVerificationError(f"publication artifact is missing: {value}")
    return resolved


def _secret_hits(content: bytes) -> bool:
    return any(pattern.search(content) for pattern in _SECRET_PATTERNS)


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items()}


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
        if path.stat().st_size > 10 * 1024 * 1024:
            continue
        if path.suffix.lower() in _TEXT_SUFFIXES or path.suffix.lower() == ".zip":
            yield path


def scan_workspace_secrets(workspace: str | Path) -> dict[str, Any]:
    root = Path(workspace).expanduser().resolve()
    findings: list[dict[str, str]] = []
    scanned = 0
    for path in _iter_scannable_files(root):
        scanned += 1
        relative = path.relative_to(root).as_posix()
        if path.suffix.lower() != ".zip":
            if _secret_hits(path.read_bytes()):
                findings.append({"path": relative, "reason": "secret pattern"})
            continue
        try:
            with zipfile.ZipFile(path) as archive:
                for member in archive.infolist():
                    if member.is_dir() or member.file_size > 10 * 1024 * 1024:
                        continue
                    if _secret_hits(archive.read(member)):
                        findings.append(
                            {
                                "path": f"{relative}!/{member.filename}",
                                "reason": "secret pattern",
                            }
                        )
        except (OSError, zipfile.BadZipFile):
            findings.append({"path": relative, "reason": "unreadable ZIP"})
    return {"clean": not findings, "scanned_files": scanned, "findings": findings}


def write_page_inspection(
    workspace: str | Path,
    *,
    pdf_path: str | Path,
    rendered_pages: Iterable[str | Path],
    reviewer: str,
) -> Path:
    root = Path(workspace).expanduser().resolve()
    pdf = Path(pdf_path).expanduser().resolve()
    pages = tuple(Path(path).expanduser().resolve() for path in rendered_pages)
    if not reviewer.strip() or not pdf.is_file() or not pages:
        raise ReleaseVerificationError("page inspection inputs are incomplete")
    payload = {
        "schema": "paperforge.page-inspection/v1",
        "status": "passed",
        "reviewer": reviewer.strip(),
        "pdf": {
            "path": pdf.relative_to(root).as_posix(),
            "sha256": sha256_file(pdf),
            "page_count": len(pages),
        },
        "pages": [
            {
                "page": index,
                "path": page.relative_to(root).as_posix(),
                "sha256": sha256_file(page),
                "result": "clean",
            }
            for index, page in enumerate(pages, start=1)
        ],
    }
    output = root / "artifacts" / "page-inspection.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return output


def _verify_page_inspection(
    workspace: Path,
    *,
    pdf_path: Path | None,
    rendered_pages: Iterable[Path],
) -> bool:
    path = workspace / "artifacts" / "page-inspection.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, Mapping):
        return False
    pdf = _mapping(payload.get("pdf"))
    page_records = payload.get("pages")
    pages = tuple(rendered_pages)
    if (
        payload.get("schema") != "paperforge.page-inspection/v1"
        or payload.get("status") != "passed"
        or not str(payload.get("reviewer", "")).strip()
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
    return actual_hashes == expected_hashes


def _load_publication_manifest(workspace: Path) -> tuple[Path, dict[str, Any]]:
    candidates = [
        path
        for path in workspace.rglob("publication.manifest.json")
        if not any(part in _IGNORED_PARTS for part in path.relative_to(workspace).parts)
    ]
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
            self.workspace / ".paperforge" / "paperforge.db"
        )

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

        gates = _mapping(manifest.get("gates"))
        artifacts = _mapping(manifest.get("artifacts"))
        project_root_value = manifest.get("project_root", ".")
        project_root = Path(str(project_root_value)).expanduser()
        if not project_root.is_absolute():
            project_root = self.workspace / project_root
        project_root = project_root.resolve()
        if (
            project_root != self.workspace
            and self.workspace not in project_root.parents
        ):
            raise ReleaseVerificationError(
                "publication project root leaves release workspace"
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
        bundle_valid = False
        try:
            bundle_path = _safe_manifest_path(
                self.workspace, bundle_record.get("path")
            )
            bundle_valid = sha256_file(bundle_path) == bundle_record.get("sha256")
            source_lock_path = _safe_manifest_path(
                self.workspace, bundle_record.get("source_lock_path")
            )
            source_lock_valid = verify_source_lock(
                project_root, source_lock_path
            ).valid
        except (OSError, ReleaseVerificationError):
            pass

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
                "bundle_verified": bundle_valid,
            }
        )
        claim_gate_record = _mapping(gates.get("claim_gate"))
        coverage_record = _mapping(claim_gate_record.get("coverage"))
        publication_gates = (
            bool(gates)
            and all(value is True for value in gates.values() if isinstance(value, bool))
            and bool(claim_gate_record.get("passed"))
            and bool(coverage_record.get("passed"))
        )
        completion = CompletionGate(
            claim_gate_passed=bool(claim_gate.get("passed")),
            required_artifacts_present=pdf_valid and bundle_valid,
            latex_clean_compile=bool(gates.get("compile")) and bool(
                gates.get("diagnostics")
            ),
            all_pdf_pages_inspected=inspection_valid,
            protected_hashes_unchanged=bool(gates.get("invariants")),
            secret_scan_clean=bool(secret_scan["clean"]),
            release_manifest_verified=(
                manifest_valid
                and publication_gates
                and source_lock_valid
            ),
            details=details,
        )
        return completion

    def write_report(self, gate: CompletionGate) -> Path:
        output = self.workspace / ".paperforge" / "release_manifest.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": "paperforge.release.manifest/v1",
            "gate": gate.to_dict(),
        }
        canonical = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        payload["sha256"] = hashlib.sha256(canonical).hexdigest()
        output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return output


__all__ = [
    "ReleaseVerificationError",
    "ReleaseVerifier",
    "scan_workspace_secrets",
    "write_page_inspection",
]
