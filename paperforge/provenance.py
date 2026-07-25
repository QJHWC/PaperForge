from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import utc_now


class ProvenanceError(RuntimeError):
    pass


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_blob_sha1(path: str | Path) -> str:
    content = Path(path).read_bytes()
    header = f"blob {len(content)}\0".encode("ascii")
    return hashlib.sha1(header + content, usedforsecurity=False).hexdigest()


@dataclass(frozen=True)
class SourceFileRecord:
    path: str
    size_bytes: int
    sha256: str
    git_blob_sha1: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "git_blob_sha1": self.git_blob_sha1,
        }


def capture_source_snapshot(
    root: str | Path,
    *,
    uri: str,
    commit: str,
    exclude_parts: Iterable[str] = (".git", "__pycache__"),
) -> dict[str, Any]:
    source_root = Path(root).expanduser().resolve()
    excluded = frozenset(exclude_parts)
    records: list[SourceFileRecord] = []
    for path in sorted(source_root.rglob("*")):
        relative = path.relative_to(source_root)
        if any(part in excluded for part in relative.parts):
            continue
        if path.is_symlink():
            raise ProvenanceError(f"source snapshot does not allow symlinks: {relative}")
        if not path.is_file():
            continue
        records.append(
            SourceFileRecord(
                path=relative.as_posix(),
                size_bytes=path.stat().st_size,
                sha256=sha256_file(path),
                git_blob_sha1=git_blob_sha1(path),
            )
        )
    if not records:
        raise ProvenanceError("source snapshot contains no files")
    tree_digest = hashlib.sha256(
        json.dumps(
            [record.to_dict() for record in records],
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return {
        "schema": "paperforge.source-snapshot/v1",
        "uri": uri,
        "commit": commit,
        "captured_at": utc_now(),
        "tree_sha256": tree_digest,
        "file_count": len(records),
        "files": [record.to_dict() for record in records],
    }
