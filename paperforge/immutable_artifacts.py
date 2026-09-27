"""Crash-safe retry and read-only integrity checks for frozen audit bundles.

Files are immutable; registration may be completed after a crash. Existing
registry/file disagreements are never healed by blindly re-registering bytes.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .artifacts import ArtifactIntegrityError, ArtifactStore
from .path_safety import atomic_write_text, reject_symlink_components


def _file(root: Path, relative: str) -> Path:
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        raise ArtifactIntegrityError("invalid immutable artifact path")
    return reject_symlink_components(root / rel, anchor=root)


def write_immutable_bundle(root: Path, files: Mapping[str, Mapping[str, Any]]) -> None:
    """Validate all existing members before filling absent ones; never rewrite."""
    missing = []
    for relative, expected in files.items():
        path = _file(root, relative)
        if path.exists():
            try:
                actual = json.loads(path.read_bytes())
                actual_stable = {k: v for k, v in actual.items() if k != "created_at"}
                expected_stable = {k: v for k, v in expected.items() if k != "created_at"}
            except (OSError, ValueError, AttributeError) as exc:
                raise ArtifactIntegrityError(f"incomplete/corrupt frozen file: {relative}") from exc
            if actual_stable != expected_stable:
                raise ArtifactIntegrityError(f"immutable content collision: {relative}")
        else:
            missing.append((relative, expected))
    for relative, content in missing:
        atomic_write_text(root, relative, json.dumps(content, ensure_ascii=False,
                                                    indent=2, sort_keys=True) + "\n")


def register_immutable(store: ArtifactStore, relative: str, *, kind: str, **kwargs: Any) -> Any:
    path = store.resolve(relative, kind=kind)
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if store.memory is not None:
        with store.memory.connect() as db:
            rows = db.execute("SELECT * FROM artifacts WHERE path=?", (relative,)).fetchall()
        if rows:
            if len(rows) != 1 or rows[0]["sha256"] != actual or rows[0]["kind"] != kind or rows[0]["status"] != "VERIFIED":
                raise ArtifactIntegrityError(f"registry disagrees with frozen file: {relative}")
            return dict(rows[0])
    return store.register(relative, kind=kind, **kwargs)


def verify_registered_bundle(root: Path, memory: Any,
                             paths: Sequence[str] | Mapping[str, str]) -> dict[str, Any]:
    findings = []
    with memory.connect() as db:
        for relative in paths:
            try:
                path = _file(root, relative)
                rows = db.execute("SELECT * FROM artifacts WHERE path=?", (relative,)).fetchall()
                if not path.is_file():
                    reason = "missing_file"
                elif not rows:
                    reason = "orphan_file"
                elif (len(rows) != 1 or rows[0]["status"] != "VERIFIED"
                      or (isinstance(paths, Mapping) and rows[0]["kind"] != paths[relative])
                      or rows[0]["sha256"] != hashlib.sha256(path.read_bytes()).hexdigest()):
                    reason = "registry_hash_or_status_mismatch"
                else:
                    continue
            except (OSError, ValueError, ArtifactIntegrityError):
                reason = "unsafe_or_unreadable_file"
            findings.append({"path": relative, "reason": reason})
    return {"registry_integrity": "valid" if not findings else "invalid",
            "registry_findings": findings, "registered_complete": not findings}
