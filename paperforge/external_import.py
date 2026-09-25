from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .evidence_semantics import EvidenceSemantics, EvidenceSemanticsError
from .models import utc_now
from .path_safety import reject_symlink_components, safe_mkdir

IMPORT_SCHEMA = "paperforge.external-import/v1"

# Caller-controlled manifest sections. System-generated fields are never
# trusted from input (manifest_sha256, validation_status, approved_at,
# evidence_ids, claim eligibility).
_SYSTEM_MANIFEST_KEYS = frozenset(
    {
        "manifest_sha256",
        "validation_status",
        "approved_at",
        "evidence_ids",
        "claim_eligible",
        "claim_eligibility",
    }
)


class ImportError(RuntimeError):
    pass


class ImportSafetyError(ImportError):
    pass


@dataclass(frozen=True)
class ImportRecord:
    import_id: str
    manifest_sha256: str
    state: str
    created_at: str
    manifest_path: str
    validation_path: str
    sources: tuple[dict[str, Any], ...]
    semantics: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _ensure_imports_table(memory: Any) -> None:
    with memory.connect() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS imports (
                id TEXT PRIMARY KEY,
                manifest_sha256 TEXT NOT NULL,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                approved_at TEXT,
                approver TEXT,
                approval_scope TEXT,
                validation_status TEXT NOT NULL,
                evidence_ids_json TEXT NOT NULL
            )
            """
        )


def _read_source_bytes(workspace: Path, raw: str | Path) -> tuple[bytes, str]:
    """Return (bytes, display_name). Copies content; never stores external paths."""
    supplied = Path(raw).expanduser()
    if supplied.is_absolute():
        if supplied.is_symlink() or (supplied.exists() and not supplied.is_file()):
            raise ImportSafetyError(f"import source must be a regular file: {raw!r}")
        if not supplied.is_file():
            raise FileNotFoundError(supplied)
        return supplied.read_bytes(), supplied.name
    lexical = reject_symlink_components(workspace / supplied, anchor=workspace)
    try:
        relative = lexical.relative_to(workspace)
    except ValueError as exc:
        raise ImportSafetyError(f"import source leaves workspace: {raw!r}") from exc
    if lexical.is_symlink() or not lexical.is_file():
        raise ImportSafetyError(f"import source must be a regular file: {relative.as_posix()}")
    return lexical.read_bytes(), Path(relative.as_posix()).name


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def stage_import(
    workspace: str | Path,
    memory: Any,
    *,
    semantics: EvidenceSemantics | Mapping[str, Any],
    subject: Mapping[str, Any],
    execution: Mapping[str, Any],
    source_files: Sequence[str | Path],
    approval_scope: str = "scientific_metric_import",
) -> ImportRecord:
    """Stage an external import. Copies raw files; creates no publishable Evidence.

    U2.1 only: state=staged, validation_status=pending. Approval and
    materialization arrive in U2.2.
    """
    root = Path(workspace).expanduser().resolve(strict=True)
    normalized = (
        semantics
        if isinstance(semantics, EvidenceSemantics)
        else EvidenceSemantics.from_metadata(dict(semantics))
    )
    try:
        normalized.validate()
    except EvidenceSemanticsError as exc:
        raise ImportError(f"invalid evidence semantics: {exc}") from exc
    if not isinstance(subject, Mapping) or not isinstance(execution, Mapping):
        raise ImportError("subject and execution must be mappings")
    for key in _SYSTEM_MANIFEST_KEYS:
        if key in dict(subject) or key in dict(execution):
            raise ImportError(f"system field must not be supplied: {key}")
    if not source_files:
        raise ImportError("at least one source file is required")
    scope = str(approval_scope or "").strip()
    if not scope:
        raise ImportError("approval_scope is required")

    import_id = f"imp_{uuid.uuid4().hex[:16]}"
    import_root = safe_mkdir(root / ".paperforge" / "imports" / import_id, anchor=root)
    sources_dir = safe_mkdir(import_root / "sources", anchor=root)
    # Copy raw materials into the canonical directory; never rely on the
    # original external absolute path afterwards.
    entries: list[dict[str, Any]] = []
    for raw in source_files:
        data, filename = _read_source_bytes(root, raw)
        if not filename.strip():
            raise ImportError("source filename is empty")
        target = sources_dir / filename
        # Avoid collisions when two inputs share a basename.
        counter = 1
        while target.exists():
            stem, suffix = Path(filename).stem, Path(filename).suffix
            target = sources_dir / f"{stem}-{counter}{suffix}"
            counter += 1
        target.write_bytes(data)
        sha = hashlib.sha256(data).hexdigest()
        suffix = Path(target.name).suffix.lower()
        media = {
            ".json": "application/json",
            ".csv": "text/csv",
            ".txt": "text/plain",
            ".md": "text/markdown",
            ".log": "text/plain",
        }.get(suffix, "application/octet-stream")
        entries.append(
            {
                "path": f"sources/{target.name}",
                "sha256": sha,
                "media_type": media,
                "size_bytes": len(data),
            }
        )
    created_at = utc_now()
    manifest: dict[str, Any] = {
        "schema": IMPORT_SCHEMA,
        "import_id": import_id,
        "created_at": created_at,
        "semantics": normalized.to_metadata(),
        "subject": dict(subject),
        "execution": dict(execution),
        "sources": entries,
        "approval": {"required": True, "scope": scope},
    }
    manifest_bytes = _canonical_json(manifest)
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    manifest_path = import_root / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    validation = {
        "schema": "paperforge.external-import.validation/v1",
        "import_id": import_id,
        "manifest_sha256": manifest_sha,
        "state": "staged",
        "validation_status": "pending",
        "checked_at": created_at,
        "detail": "staged only; validation and approval arrive in U2.2",
    }
    validation_path = import_root / "validation.json"
    validation_path.write_text(
        json.dumps(validation, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _ensure_imports_table(memory)
    with memory.connect() as db:
        db.execute(
            """
            INSERT INTO imports
            (id, manifest_sha256, state, created_at, approved_at, approver,
             approval_scope, validation_status, evidence_ids_json)
            VALUES (?, ?, 'staged', ?, NULL, NULL, ?, 'pending', '[]')
            """,
            (import_id, manifest_sha, created_at, scope),
        )
    relative_manifest = manifest_path.relative_to(root).as_posix()
    relative_validation = validation_path.relative_to(root).as_posix()
    return ImportRecord(
        import_id=import_id,
        manifest_sha256=manifest_sha,
        state="staged",
        created_at=created_at,
        manifest_path=relative_manifest,
        validation_path=relative_validation,
        sources=tuple(entries),
        semantics=normalized.to_metadata(),
    )


def get_import(workspace: str | Path, memory: Any, import_id: str) -> dict[str, Any]:
    _ensure_imports_table(memory)
    with memory.connect() as db:
        row = db.execute("SELECT * FROM imports WHERE id = ?", (import_id,)).fetchone()
    if row is None:
        raise KeyError(import_id)
    root = Path(workspace).expanduser().resolve(strict=True)
    manifest_path = root / ".paperforge" / "imports" / import_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return {"record": dict(row), "manifest": manifest}


__all__ = [
    "IMPORT_SCHEMA",
    "ImportError",
    "ImportRecord",
    "ImportSafetyError",
    "get_import",
    "stage_import",
]
