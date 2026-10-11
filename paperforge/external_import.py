from __future__ import annotations

import contextlib
import hashlib
import json
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .evidence_semantics import EvidenceSemantics, EvidenceSemanticsError
from .models import utc_now
from .path_safety import (
    atomic_write_bytes,
    atomic_write_text,
    reject_symlink_components,
    safe_mkdir,
    safe_output_path,
)

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
        atomic_write_bytes(root, target.relative_to(root), data)
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
    atomic_write_text(root, manifest_path.relative_to(root),
                      json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
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
    atomic_write_text(root, validation_path.relative_to(root),
                      json.dumps(validation, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
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


VALIDATION_SCHEMA = "paperforge.external-import.validation/v1"
MATERIALIZATION_VERSION = "v1"


class ValidationError(ImportError):
    pass


class ApprovalError(ImportError):
    pass


class ConflictError(ImportError):
    def __init__(self, message: str, *, report: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.conflict_report = report or {}


@dataclass(frozen=True)
class ApprovalBinding:
    import_id: str
    manifest_sha256: str
    validation_report_sha256: str
    approval_scope: str
    approver: str
    approved_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _ensure_u22_tables(memory: Any) -> None:
    _ensure_imports_table(memory)
    with memory.connect() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS import_approvals (
                id TEXT PRIMARY KEY,
                import_id TEXT NOT NULL,
                manifest_sha256 TEXT NOT NULL,
                validation_report_sha256 TEXT NOT NULL,
                approval_scope TEXT NOT NULL,
                approver TEXT NOT NULL,
                approved_at TEXT NOT NULL,
                acknowledged_checks_json TEXT NOT NULL,
                rationale TEXT NOT NULL,
                status TEXT NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS import_materializations (
                key TEXT PRIMARY KEY,
                import_id TEXT NOT NULL,
                manifest_sha256 TEXT NOT NULL,
                validation_report_sha256 TEXT NOT NULL,
                approval_scope TEXT NOT NULL,
                evidence_ids_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )


def _import_paths(root: Path, import_id: str) -> tuple[Path, Path, Path]:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", import_id):
        raise ImportSafetyError("invalid import identifier")
    base = root / ".paperforge" / "imports" / import_id
    for name in ("manifest.json", "validation.json", "sources"):
        reject_symlink_components(base / name, anchor=root)
    return base / "manifest.json", base / "validation.json", base / "sources"


def _read_canonical_manifest(manifest_path: Path) -> tuple[dict[str, Any], str]:
    raw = manifest_path.read_bytes()
    manifest = json.loads(raw.decode("utf-8"))
    canonical = _canonical_json(manifest)
    return manifest, hashlib.sha256(canonical).hexdigest()


RUN_IDENTITY_SCHEMA = "paperforge.scientific-run-identity/v1"


@dataclass(frozen=True)
class ScientificRunIdentity:
    """Canonical conflict/duplicate identity. Versioned so history never shifts."""

    schema: str = RUN_IDENTITY_SCHEMA
    model_id: str = ""
    checkpoint_sha256: str = ""
    dataset_id: str = ""
    split_sha256: str = ""
    seed: str = ""
    device_id: str = ""
    runtime: str = ""
    runtime_version: str = ""
    precision: str = ""
    input_size: str = ""
    batch_size: str = ""
    timing_scope: str = ""
    measurement_kind: str = ""

    @classmethod
    def from_manifest(cls, manifest: Mapping[str, Any]) -> ScientificRunIdentity:
        subject = dict(manifest.get("subject") or {})
        execution = dict(manifest.get("execution") or {})
        semantics = dict(manifest.get("semantics") or {})

        def _s(mapping: Mapping[str, Any], key: str) -> str:
            value = mapping.get(key)
            return "" if value is None else str(value).strip()

        return cls(
            model_id=_s(subject, "model_id"),
            checkpoint_sha256=_s(subject, "checkpoint_sha256"),
            dataset_id=_s(subject, "dataset_id"),
            split_sha256=_s(subject, "split_sha256"),
            seed=_s(subject, "seed"),
            device_id=_s(execution, "device") or _s(execution, "device_id"),
            runtime=_s(execution, "runtime"),
            runtime_version=_s(execution, "runtime_version"),
            precision=_s(execution, "precision"),
            input_size=_s(execution, "input_size") or _s(subject, "input_size"),
            batch_size=_s(execution, "batch_size") or _s(subject, "batch_size"),
            timing_scope=_s(execution, "timing_scope"),
            measurement_kind=_s(semantics, "measurement_kind"),
        )

    def canonical(self) -> tuple[str, ...]:
        return (
            self.schema,
            self.model_id,
            self.checkpoint_sha256,
            self.dataset_id,
            self.split_sha256,
            self.seed,
            self.device_id,
            self.runtime,
            self.runtime_version,
            self.precision,
            self.input_size,
            self.batch_size,
            self.timing_scope,
            self.measurement_kind,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _run_identity(manifest: Mapping[str, Any]) -> tuple[str, ...]:
    return ScientificRunIdentity.from_manifest(manifest).canonical()


# F1: stable digest excludes volatile timestamps and itself.
_STABLE_VALIDATION_KEYS = (
    "schema",
    "import_id",
    "manifest_sha256",
    "source_hashes",
    "semantics_result",
    "checks",
    "overall_status",
    "duplicate_of",
    "conflict_with",
    "run_identity",
    "run_identity_schema",
)


def _stable_validation_payload(report: Mapping[str, Any]) -> dict[str, Any]:
    return {key: report.get(key) for key in _STABLE_VALIDATION_KEYS}


def _validation_digest(stable: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(dict(stable))).hexdigest()


def _check_entry(
    check_id: str, status: str, detail: str = "", evidence_needed: str = ""
) -> dict[str, Any]:
    entry: dict[str, Any] = {"check_id": check_id, "status": status, "detail": detail}
    if evidence_needed:
        entry["evidence_needed"] = evidence_needed
    return entry


def validate_import(workspace: str | Path, memory: Any, import_id: str) -> dict[str, Any]:
    """Recompute everything from canonical files. Never trusts DB strings."""
    root = Path(workspace).expanduser().resolve(strict=True)
    manifest_path, validation_path, sources_dir = _import_paths(root, import_id)
    if not manifest_path.is_file():
        raise ValidationError(f"manifest missing: {import_id}")
    manifest, current_manifest_sha = _read_canonical_manifest(manifest_path)
    checks: list[dict[str, Any]] = []
    if manifest.get("schema") != IMPORT_SCHEMA:
        checks.append(_check_entry("schema_valid", "failed", f"schema={manifest.get('schema')}"))
    else:
        checks.append(_check_entry("schema_valid", "passed", IMPORT_SCHEMA))
    # Required metadata (minimal, auditable).
    subject_value = manifest.get("subject")
    execution_value = manifest.get("execution")
    subject = subject_value if isinstance(subject_value, Mapping) else {}
    execution = execution_value if isinstance(execution_value, Mapping) else {}
    for field in ("model_id", "dataset_id"):
        if str(subject.get(field) or "").strip():
            checks.append(_check_entry(f"subject.{field}", "passed", str(subject[field])[:120]))
        else:
            checks.append(_check_entry(f"subject.{field}", "failed", "missing required subject field"))
    for field in ("device", "timing_scope"):
        if str(execution.get(field) or "").strip():
            checks.append(_check_entry(f"execution.{field}", "passed", str(execution[field])[:120]))
        else:
            checks.append(_check_entry(f"execution.{field}", "failed", "missing required execution field"))
    # Semantics.
    try:
        semantics = EvidenceSemantics.from_metadata(dict(manifest.get("semantics") or {}))
        semantics.validate()
        checks.append(
            _check_entry(
                "semantics_valid",
                "passed",
                f"{semantics.measurement_kind}/{semantics.acquisition_origin}/{semantics.environment_role}",
            )
        )
        if semantics.is_semantically_admissible():
            checks.append(_check_entry("semantics_admissible", "passed", "scientific candidate"))
        else:
            checks.append(
                _check_entry(
                    "semantics_admissible",
                    "failed",
                    "fixture/synthetic/transcribed are never publication candidates",
                )
            )
    except EvidenceSemanticsError as exc:
        semantics = None
        checks.append(_check_entry("semantics_valid", "failed", str(exc)))
        checks.append(_check_entry("semantics_admissible", "failed", "invalid semantics"))
    # Sources: existence + hash recomputation.
    sources_value = manifest.get("sources")
    declared = sources_value if isinstance(sources_value, list) else []
    actual_hashes: dict[str, str] = {}
    if not declared:
        checks.append(_check_entry("sources_present", "failed", "no sources declared"))
    for entry in declared:
        if not isinstance(entry, Mapping):
            checks.append(_check_entry("source_entry", "failed", "invalid source entry"))
            continue
        rel = str(entry.get("path") or "")
        expected = str(entry.get("sha256") or "")
        candidate = (sources_dir.parent / rel) if rel else None
        if candidate is None or not candidate.is_file() or candidate.is_symlink():
            checks.append(_check_entry(f"source_exists:{rel}", "failed", "source file missing"))
            continue
        actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
        actual_hashes[rel] = actual
        if actual == expected.lower():
            checks.append(_check_entry(f"source_hash:{rel}", "passed", actual[:16]))
        else:
            checks.append(
                _check_entry(
                    f"source_hash:{rel}", "failed", f"expected {expected[:16]} got {actual[:16]}"
                )
            )
    # Unit/measurement basic compatibility: energy claims require power input.
    # The manifest does not declare metrics; materialize-time metric checks
    # enforce power requirements. Here we only ensure timing_scope is sane.
    timing = str(execution.get("timing_scope") or "")
    if timing and timing not in {"inference_only", "end_to_end", "training_step"}:
        checks.append(_check_entry("timing_scope", "failed", f"unknown timing_scope: {timing}"))
    elif timing:
        checks.append(_check_entry("timing_scope", "passed", timing))
    # Device identity can never be auto-proven from a CSV.
    checks.append(
        _check_entry(
            "device_identity",
            "needs_human",
            "CSV/JSON cannot prove which physical device produced the log",
            evidence_needed="operator attestation: device serial, setup photo or inventory record",
        )
    )
    # Duplicate vs conflict against prior staged/validated imports.
    _ensure_u22_tables(memory)
    identity = _run_identity(manifest)
    with memory.connect() as db:
        rows = db.execute("SELECT id, manifest_sha256 FROM imports WHERE id != ?", (import_id,)).fetchall()
    duplicate_of: str | None = None
    conflict_with: list[str] = []
    for row in rows:
        other_id = str(row["id"])
        other_manifest_path = root / ".paperforge" / "imports" / other_id / "manifest.json"
        if not other_manifest_path.is_file():
            continue
        try:
            other_manifest, _ = _read_canonical_manifest(other_manifest_path)
        except (OSError, ValueError):
            continue
        if _run_identity(other_manifest) != identity:
            continue
        other_sources_value = other_manifest.get("sources")
        other_sources = other_sources_value if isinstance(other_sources_value, list) else []
        other_hashes = sorted(str(e.get("sha256") or "") for e in other_sources if isinstance(e, Mapping))
        my_hashes = sorted(str(e.get("sha256") or "") for e in declared if isinstance(e, Mapping))
        if (
            other_manifest.get("semantics") == manifest.get("semantics")
            and other_manifest.get("subject") == manifest.get("subject")
            and other_manifest.get("execution") == manifest.get("execution")
            and other_hashes == my_hashes
        ):
            duplicate_of = other_id
        else:
            conflict_with.append(other_id)
    if duplicate_of:
        checks.append(_check_entry("duplicate", "passed", f"identical to {duplicate_of}; idempotent"))
    if conflict_with:
        checks.append(
            _check_entry(
                "conflict",
                "needs_human",
                f"same run identity as {','.join(conflict_with[:3])} with different content; do not overwrite/average/latest",
                evidence_needed="human decision: which measurement run is authoritative",
            )
        )
    failed = any(c["status"] == "failed" for c in checks)
    needs_human = any(c["status"] == "needs_human" for c in checks)
    overall = "failed" if failed else ("needs_human" if needs_human else "passed")
    run_identity = ScientificRunIdentity.from_manifest(manifest)
    stable: dict[str, Any] = {
        "schema": VALIDATION_SCHEMA,
        "import_id": import_id,
        "manifest_sha256": current_manifest_sha,
        "source_hashes": actual_hashes,
        "semantics_result": (semantics.to_metadata() if semantics else {}),
        "checks": checks,
        "overall_status": overall,
        "duplicate_of": duplicate_of,
        "conflict_with": conflict_with,
        "run_identity": run_identity.to_dict(),
        "run_identity_schema": RUN_IDENTITY_SCHEMA,
    }
    digest = _validation_digest(stable)
    # Idempotent re-validate: same stable content returns the same digest
    # without depending on volatile timestamps.
    if validation_path.is_file():
        try:
            existing = json.loads(validation_path.read_bytes().decode("utf-8"))
            existing_digest = str(
                existing.get("validation_digest")
                or existing.get("validation_report_sha256")
                or ""
            )
            if existing_digest == digest and existing.get("manifest_sha256") == current_manifest_sha:
                state = "validated" if overall in {"passed", "needs_human"} else "validation_failed"
                with memory.connect() as db:
                    db.execute(
                        "UPDATE imports SET state = ?, validation_status = ? WHERE id = ?",
                        (state, overall, import_id),
                    )
                return existing
        except (OSError, ValueError):
            pass
    report: dict[str, Any] = {
        **stable,
        "validation_digest": digest,
        # Legacy alias: approval/materialization bindings historically read
        # validation_report_sha256. It now stores the stable digest (not a
        # self-referential file hash) for backward compatibility.
        "validation_report_sha256": digest,
        "created_at": utc_now(),
        "validated_at": utc_now(),
    }
    # Retain suspicious legacy temp paths; use an exclusive random temp for writes.
    tmp_path = validation_path.with_name(f".{validation_path.name}.{import_id}.tmp")
    safe_output_path(root, tmp_path.relative_to(root))
    atomic_write_text(root, validation_path.relative_to(root),
                      json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    state = "validated" if overall in {"passed", "needs_human"} else "validation_failed"
    with memory.connect() as db:
        db.execute(
            "UPDATE imports SET state = ?, validation_status = ? WHERE id = ?",
            (state, overall, import_id),
        )
    return report


def _current_validation_report(root: Path, import_id: str) -> tuple[dict[str, Any], str]:
    """Return (report, digest). Verifies digest + file integrity, not timestamps."""
    _, validation_path, _ = _import_paths(root, import_id)
    if not validation_path.is_file():
        raise ApprovalError("validation report missing; run validate first")
    raw = validation_path.read_bytes()
    try:
        report = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise ApprovalError(f"validation report is not valid JSON: {exc}") from exc
    stored_digest = str(
        report.get("validation_digest") or report.get("validation_report_sha256") or ""
    )
    if not stored_digest:
        raise ApprovalError("validation report has no digest")
    recomputed = _validation_digest(_stable_validation_payload(report))
    if stored_digest != recomputed:
        raise ApprovalError("validation report was modified after creation")
    # File integrity: any byte change to stable fields breaks the digest above.
    # Timestamp-only changes keep the digest stable and do not invalidate.
    return report, stored_digest


def _validation_file_sha256(root: Path, import_id: str) -> str:
    _, validation_path, _ = _import_paths(root, import_id)
    return hashlib.sha256(validation_path.read_bytes()).hexdigest()


def _insert_evidence_row(
    db: Any, *, import_id: str, rel: str, excerpt: str, metadata: dict[str, Any]
) -> str:
    """Insert one external evidence row inside the caller's transaction.

    Module-level for exception-injection tests. Production crash safety comes
    from the single-transaction materialize block, not from post-hoc deletes.
    """
    from engine.secret_redaction import contains_secret as _contains_secret

    from .scientific_memory import _stable_id as _evidence_stable_id

    if _contains_secret({"excerpt": excerpt, "metadata": metadata}):
        raise ValueError("evidence must not contain credentials")
    content_sha = hashlib.sha256(excerpt.encode("utf-8")).hexdigest()
    evidence_path = f".paperforge/imports/{import_id}/{rel}"
    evidence_id = _evidence_stable_id(
        "ev", "EXTERNAL_MEASUREMENT", "", evidence_path, "", "", content_sha
    )
    db.execute(
        """
        INSERT INTO evidence
        (id, evidence_type, source_id, path, line_start, line_end, excerpt,
         content_sha256, config_scope, captured_at, metadata_json)
        VALUES (?, ?, NULL, ?, NULL, NULL, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            excerpt=excluded.excerpt,
            content_sha256=excluded.content_sha256,
            metadata_json=excluded.metadata_json
        """,
        (
            evidence_id,
            "EXTERNAL_MEASUREMENT",
            evidence_path,
            excerpt,
            content_sha,
            f"external-import:{import_id}",
            utc_now(),
            json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        ),
    )
    return evidence_id


def approve_import(
    workspace: str | Path,
    memory: Any,
    import_id: str,
    *,
    approver: str = "local-owner",
    approval_scope: str | None = None,
    acknowledged_checks: Sequence[str] | None = None,
    rationale: str = "",
) -> dict[str, Any]:
    """Explicit local-owner approval bound to both hashes. Recomputes files."""
    root = Path(workspace).expanduser().resolve(strict=True)
    manifest_path, _, sources_dir = _import_paths(root, import_id)
    manifest, current_manifest_sha = _read_canonical_manifest(manifest_path)
    report, report_sha = _current_validation_report(root, import_id)
    if report.get("manifest_sha256") != current_manifest_sha:
        raise ApprovalError("manifest changed after validation; approval refused")
    # Sources must still match the manifest.
    for entry in manifest.get("sources") or []:
        rel = str(entry.get("path") or "")
        expected = str(entry.get("sha256") or "")
        candidate = sources_dir.parent / rel
        if not candidate.is_file():
            raise ApprovalError(f"source missing at approval: {rel}")
        if hashlib.sha256(candidate.read_bytes()).hexdigest() != expected.lower():
            raise ApprovalError(f"source changed after validation: {rel}")
    overall = str(report.get("overall_status") or "")
    if overall == "failed":
        raise ApprovalError("validation failed; approval is forbidden")
    acknowledged = [str(c) for c in (acknowledged_checks or [])]
    if overall == "needs_human":
        required = [c["check_id"] for c in report.get("checks", []) if c.get("status") == "needs_human"]
        missing = [c for c in required if c not in acknowledged]
        if missing:
            raise ApprovalError(f"needs_human checks require acknowledgement: {missing}")
        if not str(rationale or "").strip():
            raise ApprovalError("needs_human approval requires a rationale")
    scope = str(approval_scope or manifest.get("approval", {}).get("scope") or "").strip()
    if not scope:
        raise ApprovalError("approval_scope is required")
    who = str(approver or "").strip()
    if not who:
        raise ApprovalError("approver is required")
    # Caller-supplied approved_at is never trusted.
    approved_at = utc_now()
    approval_id = f"appr_{hashlib.sha256(f'{import_id}{current_manifest_sha}{report_sha}{scope}{who}{approved_at}'.encode()).hexdigest()[:16]}"
    binding = ApprovalBinding(
        import_id=import_id,
        manifest_sha256=current_manifest_sha,
        validation_report_sha256=report_sha,
        approval_scope=scope,
        approver=who,
        approved_at=approved_at,
    )
    _ensure_u22_tables(memory)
    with memory.connect() as db:
        db.execute(
            """
            INSERT INTO import_approvals
            (id, import_id, manifest_sha256, validation_report_sha256,
             approval_scope, approver, approved_at, acknowledged_checks_json,
             rationale, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'valid')
            """,
            (
                approval_id,
                import_id,
                current_manifest_sha,
                report_sha,
                scope,
                who,
                approved_at,
                json.dumps(sorted(acknowledged), ensure_ascii=False, sort_keys=True),
                str(rationale or ""),
            ),
        )
        db.execute(
            "UPDATE imports SET state='approved', approved_at=?, approver=?, approval_scope=? WHERE id=?",
            (approved_at, who, scope, import_id),
        )
    return {"approval_id": approval_id, "binding": binding.to_dict()}


def approval_valid(workspace: str | Path, memory: Any, import_id: str) -> tuple[bool, str]:
    """Recompute current hashes; old approvals stay but become invalid on change."""
    root = Path(workspace).expanduser().resolve(strict=True)
    _ensure_u22_tables(memory)
    with memory.connect() as db:
        row = db.execute(
            "SELECT * FROM import_approvals WHERE import_id=? AND status='valid' ORDER BY approved_at DESC LIMIT 1",
            (import_id,),
        ).fetchone()
    if row is None:
        return False, "no valid approval"
    manifest_path, _, sources_dir = _import_paths(root, import_id)
    try:
        manifest, current_manifest_sha = _read_canonical_manifest(manifest_path)
        _, current_report_sha = _current_validation_report(root, import_id)
    except (OSError, ValueError, ApprovalError) as exc:
        return False, f"recomputation failed: {exc}"
    if current_manifest_sha != str(row["manifest_sha256"]):
        return False, "manifest changed; approval invalid"
    if current_report_sha != str(row["validation_report_sha256"]):
        return False, "validation report changed; approval invalid"
    for entry in manifest.get("sources") or []:
        rel = str(entry.get("path") or "")
        expected = str(entry.get("sha256") or "")
        candidate = sources_dir.parent / rel
        if not candidate.is_file() or hashlib.sha256(candidate.read_bytes()).hexdigest() != expected.lower():
            return False, f"source changed: {rel}"
    return True, "approval valid"


def _invalidate_approval(memory: Any, import_id: str, reason: str) -> None:
    with memory.connect() as db:
        db.execute(
            "UPDATE import_approvals SET status='invalidated' WHERE import_id=? AND status='valid'",
            (import_id,),
        )
        db.execute("UPDATE imports SET state='invalidated' WHERE id=?", (import_id,))


def _materialization_key(manifest_sha: str, report_sha: str, scope: str) -> str:
    return hashlib.sha256(
        f"{manifest_sha}\x1f{report_sha}\x1f{scope}\x1f{MATERIALIZATION_VERSION}".encode()
    ).hexdigest()


def _validate_metric_provenance(
    semantics: EvidenceSemantics,
    metric: str | None,
    metric_provenance: Mapping[str, Any] | None,
) -> None:
    """Enforce acquisition-method semantics, not metric-name alone.

    Reciprocal FPS and fixed-power energy are derived proxies even when their
    parents are measured. Windowed throughput and power-trace integration may
    be measured, but only with matching provenance.
    """
    if metric is None and metric_provenance is None:
        return
    provenance = dict(metric_provenance or {})
    declared_metric = str(provenance.get("metric") or metric or "").strip().lower()
    method = str(provenance.get("acquisition_method") or "").strip().lower()
    if not declared_metric and not method:
        return
    # Legacy string-only calls: measured FPS/energy now require explicit method
    # instead of being blanket-forced to derived.
    if not method:
        if declared_metric == "measured_energy" and semantics.measurement_kind != "measured":
            raise ApprovalError(
                f"metric {metric!r} must be recorded as derived with parents, not measured"
            )
        if declared_metric in {"fps", "energy"} and semantics.measurement_kind == "measured":
            raise ApprovalError(
                f"metric {declared_metric!r} with measured semantics requires explicit "
                "acquisition_method (reciprocal vs windowed/trace); refusing ambiguous claim"
            )
        return
    if declared_metric in {"latency", "latency_ms"}:
        if method in {"timer_samples", "direct_timer"}:
            if semantics.measurement_kind != "measured":
                raise ApprovalError("timer-sampled latency must be measured")
            return
        raise ApprovalError(f"unknown latency acquisition_method: {method!r}")
    if declared_metric == "fps":
        if method in {"fps_from_latency", "reciprocal_latency"}:
            if semantics.measurement_kind != "derived" or not semantics.parent_evidence_ids:
                raise ApprovalError("reciprocal FPS must be derived with latency parents")
            return
        if method in {"windowed_throughput_measurement", "frame_count_window"}:
            if semantics.measurement_kind != "measured":
                raise ApprovalError("windowed throughput must be measured, not derived")
            frames = provenance.get("frame_count")
            elapsed = provenance.get("elapsed_time_s")
            if not isinstance(frames, int) or frames <= 0:
                raise ApprovalError("windowed FPS requires positive frame_count")
            if not isinstance(elapsed, int | float) or float(elapsed) <= 0:
                raise ApprovalError("windowed FPS requires positive elapsed_time_s")
            return
        raise ApprovalError(f"unknown FPS acquisition_method: {method!r}")
    if declared_metric in {"energy", "energy_per_frame", "measured_energy"}:
        if method in {"fixed_power_energy_proxy", "fixed_power_times_latency"}:
            if semantics.measurement_kind != "derived" or not semantics.parent_evidence_ids:
                raise ApprovalError("fixed-power energy must be derived with parents, never measured_energy")
            return
        if method in {"power_trace_integration", "integrated_power_trace"}:
            if semantics.measurement_kind != "measured":
                raise ApprovalError("power-trace energy must be measured, not derived")
            if not str(provenance.get("power_trace_ref") or "").strip():
                raise ApprovalError("power-trace energy requires power_trace_ref")
            if not str(provenance.get("measurement_window") or "").strip():
                raise ApprovalError("power-trace energy requires measurement_window")
            frames = provenance.get("frame_count")
            if not isinstance(frames, int) or frames <= 0:
                raise ApprovalError("power-trace energy requires positive frame_count")
            return
        raise ApprovalError(f"unknown energy acquisition_method: {method!r}")
    # Unknown metrics: do not silently allow measured claims without provenance.
    if semantics.measurement_kind == "measured" and not method:
        raise ApprovalError(f"measured metric {declared_metric!r} requires acquisition_method")


def materialize_import(
    workspace: str | Path,
    memory: Any,
    import_id: str,
    *,
    metric: str | None = None,
    metric_provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create Evidence only from approved+unchanged imports. Idempotent by key."""
    root = Path(workspace).expanduser().resolve(strict=True)
    _ensure_u22_tables(memory)
    valid, reason = approval_valid(workspace, memory, import_id)
    if not valid:
        # Preserve the old approval row; mark state for audit and refuse.
        with contextlib.suppress(OSError):
            _invalidate_approval(memory, import_id, reason)
        raise ApprovalError(f"approval invalid: {reason}")
    manifest_path, _, _ = _import_paths(root, import_id)
    manifest, manifest_sha = _read_canonical_manifest(manifest_path)
    _, report_sha = _current_validation_report(root, import_id)
    with memory.connect() as db:
        approval = db.execute(
            "SELECT * FROM import_approvals WHERE import_id=? AND status='valid' ORDER BY approved_at DESC LIMIT 1",
            (import_id,),
        ).fetchone()
    if approval is None:
        raise ApprovalError("no valid approval")
    scope = str(approval["approval_scope"])
    semantics = EvidenceSemantics.from_metadata(dict(manifest.get("semantics") or {}))
    # Publication-eligibility gate: fixture/synthetic/transcribed never materialize
    # as scientific evidence; derived/remapped require lineage (already validated).
    if not semantics.is_semantically_admissible():
        raise ApprovalError(
            f"semantics not admissible for scientific evidence: {semantics.measurement_kind}/{semantics.environment_role}"
        )
    _validate_metric_provenance(semantics, metric, metric_provenance)
    key = _materialization_key(manifest_sha, report_sha, scope)
    consistency = reconcile_imports(root, memory)
    if not consistency["consistent"]:
        # Do not recreate orphan Evidence or overwrite a damaged historical
        # row while retrying an otherwise valid import.
        raise ApprovalError("materialization integrity mismatch; manual reconciliation required")
    with memory.connect() as db:
        existing = db.execute(
            "SELECT * FROM import_materializations WHERE key=?", (key,)
        ).fetchone()
    if existing is not None:
        consistency = reconcile_imports(root, memory)
        if (key in consistency["orphan_materializations"]
                or import_id in consistency["state_mismatches"]
                or any(eid in consistency["orphan_evidence"]
                       for eid in json.loads(existing["evidence_ids_json"]))):
            raise ApprovalError("materialization integrity mismatch; manual reconciliation required")
        return {
            "import_id": import_id,
            "materialization_key": key,
            "evidence_ids": json.loads(str(existing["evidence_ids_json"])),
            "idempotent": True,
        }
    # Conflict check at materialize time (same identity, different content).
    identity = _run_identity(manifest)
    with memory.connect() as db:
        rows = db.execute("SELECT id FROM imports WHERE id != ?", (import_id,)).fetchall()
    for row in rows:
        other_id = str(row["id"])
        other_path = root / ".paperforge" / "imports" / other_id / "manifest.json"
        if not other_path.is_file():
            continue
        try:
            other_manifest, _ = _read_canonical_manifest(other_path)
        except (OSError, ValueError):
            continue
        if _run_identity(other_manifest) != identity:
            continue
        with memory.connect() as db2:
            mat = db2.execute(
                "SELECT * FROM import_materializations WHERE import_id=?", (other_id,)
            ).fetchone()
        if mat is not None:
            raise ConflictError(
                f"conflicting measurement for run identity {identity[:3]}: {other_id} already materialized; human decision required",
                report={"conflict_with": other_id, "run_identity": list(identity)},
            )
    # Single SQLite transaction: evidence + materialization + import state.
    # A crash before COMMIT rolls everything back (WAL atomic commit).
    import paperforge.external_import as _self_for_transaction

    created: list[str] = []
    with memory.connect() as db:
        for entry in manifest.get("sources") or []:
            rel = str(entry.get("path") or "")
            excerpt = (
                f"external import {import_id} source {rel} "
                f"sha256={entry.get('sha256')} "
                f"semantics={semantics.measurement_kind}/{semantics.acquisition_origin}/{semantics.environment_role}"
            )
            metadata = {
                **semantics.to_metadata(),
                "import_id": import_id,
                "manifest_sha256": manifest_sha,
                "validation_digest": report_sha,
                "validation_report_sha256": report_sha,
                "approval_scope": scope,
                "source_relpath": rel,
                "source_sha256": entry.get("sha256"),
                "materialization_key": key,
                "scientifically_reviewed": False,
                "claim_eligible": True,
            }
            created.append(
                _self_for_transaction._insert_evidence_row(
                    db, import_id=import_id, rel=rel, excerpt=excerpt, metadata=metadata
                )
            )
        db.execute(
            """
            INSERT INTO import_materializations
            (key, import_id, manifest_sha256, validation_report_sha256,
             approval_scope, evidence_ids_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (key, import_id, manifest_sha, report_sha, scope, json.dumps(created), utc_now()),
        )
        db.execute(
            "UPDATE imports SET state='materialized', evidence_ids_json=? WHERE id=?",
            (json.dumps(created), import_id),
        )
    return {
        "import_id": import_id,
        "materialization_key": key,
        "evidence_ids": created,
        "idempotent": False,
    }


def reconcile_imports(workspace: str | Path, memory: Any, *, repair: bool = False) -> dict[str, Any]:
    """Detect orphan evidence/materializations and state mismatches.

    Unknown orphans are retained, not deleted or recreated. Reconciliation
    never creates Evidence, changes claims, or issues an approval.
    """
    root = Path(workspace).expanduser().resolve(strict=True)
    with memory.connect() as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        materials = db.execute("SELECT * FROM import_materializations").fetchall() if "import_materializations" in tables else []
        evidence_rows = db.execute("SELECT * FROM evidence").fetchall()
        imports = db.execute("SELECT * FROM imports").fetchall() if "imports" in tables else []
    materialized_keys = {str(r["key"]): r for r in materials}
    evidence_by_id = {str(r["id"]): r for r in evidence_rows}
    orphan_evidence: list[str] = []
    for evidence_id, row in evidence_by_id.items():
        try:
            meta = json.loads(str(row["metadata_json"]))
        except (TypeError, ValueError):
            continue
        if not isinstance(meta, dict) or "materialization_key" not in meta:
            continue
        material = materialized_keys.get(str(meta["materialization_key"]))
        try:
            referenced = json.loads(material["evidence_ids_json"]) if material else []
        except (TypeError, ValueError):
            referenced = []
        if material is None or evidence_id not in referenced:
            orphan_evidence.append(evidence_id)
    orphan_materializations: list[str] = []
    for key, row in materialized_keys.items():
        try:
            evidence_ids = json.loads(str(row["evidence_ids_json"]))
        except (TypeError, ValueError):
            evidence_ids = []
        if (not isinstance(evidence_ids, list) or not evidence_ids
                or any(not isinstance(eid, str) or not eid for eid in evidence_ids)
                or len(evidence_ids) != len(set(evidence_ids))
                or any(str(eid) not in evidence_by_id for eid in evidence_ids)
                or not any(r["id"] == row["import_id"] for r in imports)):
            orphan_materializations.append(key)
            continue
        for eid in evidence_ids:
            try:
                evidence = evidence_by_id[eid]
                meta = json.loads(evidence["metadata_json"])
                manifest_path = root / ".paperforge/imports" / row["import_id"] / "manifest.json"
                manifest, manifest_sha = _read_canonical_manifest(manifest_path)
                source = next((s for s in manifest.get("sources", [])
                               if s.get("path") == meta.get("source_relpath")), None)
                if (meta.get("materialization_key") != key or meta.get("import_id") != row["import_id"]
                        or evidence["content_sha256"] != hashlib.sha256(evidence["excerpt"].encode()).hexdigest()
                        or meta.get("manifest_sha256") != row["manifest_sha256"] or manifest_sha != row["manifest_sha256"]
                        or meta.get("validation_digest") != row["validation_report_sha256"]
                        or meta.get("approval_scope") != row["approval_scope"]
                        or _materialization_key(row["manifest_sha256"], row["validation_report_sha256"], row["approval_scope"]) != key
                        or not source or meta.get("source_sha256") != source.get("sha256")):
                    orphan_materializations.append(key)
                    break
            except (OSError, TypeError, ValueError, KeyError, AttributeError):
                orphan_materializations.append(key)
                break
    mismatches: list[str] = []
    for row in imports:
        import_id = str(row["id"])
        manifest_path = root / ".paperforge" / "imports" / import_id / "manifest.json"
        state = str(row["state"])
        try:
            evidence_ids = json.loads(str(row["evidence_ids_json"] or "[]"))
        except (TypeError, ValueError):
            evidence_ids = []
        matching = [m for m in materials if m["import_id"] == import_id]
        valid_match = (len(matching) == 1 and matching[0]["key"] not in orphan_materializations
                       and json.loads(matching[0]["evidence_ids_json"]) == evidence_ids
                       and manifest_path.is_file())
        if (state == "materialized" and not valid_match) or (matching and state != "materialized"):
            mismatches.append(import_id)
    report = {
        "orphan_evidence": sorted(orphan_evidence),
        "orphan_materializations": sorted(orphan_materializations),
        "state_mismatches": sorted(mismatches),
        "consistent": not orphan_evidence and not orphan_materializations and not mismatches,
        "recovery_outcome": "MANUAL_INTERVENTION_REQUIRED" if orphan_evidence or orphan_materializations or mismatches else "CLEAN",
        "repaired": False,
    }
    if repair and not report["consistent"]:
        report["repair_refused"] = "ambiguous historical state retained; no Evidence or approval mutations"
    return report


__all__ = [
    "IMPORT_SCHEMA",
    "RUN_IDENTITY_SCHEMA",
    "ApprovalBinding",
    "ApprovalError",
    "ConflictError",
    "ImportError",
    "ImportRecord",
    "ImportSafetyError",
    "ScientificRunIdentity",
    "VALIDATION_SCHEMA",
    "_insert_evidence_row",
    "approval_valid",
    "approve_import",
    "get_import",
    "materialize_import",
    "reconcile_imports",
    "stage_import",
    "validate_import",
]
