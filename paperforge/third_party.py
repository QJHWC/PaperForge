from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ThirdPartyIntegrityError(RuntimeError):
    pass


@dataclass(frozen=True)
class ThirdPartyVerification:
    valid: bool
    checked_files: int
    sources: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "checked_files": self.checked_files,
            "sources": list(self.sources),
        }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_third_party_lock(repo_root: str | Path) -> ThirdPartyVerification:
    root = Path(repo_root).expanduser().resolve()
    lock_path = root / "third_party" / "source-lock.json"
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ThirdPartyIntegrityError("third-party source lock is unreadable") from exc
    if lock.get("schema") != "paperforge.third-party-lock/v1":
        raise ThirdPartyIntegrityError("unsupported third-party source lock schema")

    sources = lock.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ThirdPartyIntegrityError("third-party source lock has no sources")
    source_names = tuple(str(source.get("name")) for source in sources if isinstance(source, dict))
    if len(source_names) != len(sources) or len(set(source_names)) != len(source_names):
        raise ThirdPartyIntegrityError("third-party source names are invalid")

    checksums = root / "third_party" / "latex-paper-skills" / "SHA256SUMS"
    checked = 0
    try:
        lines = checksums.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ThirdPartyIntegrityError("vendored checksum manifest is missing") from exc
    for line in lines:
        digest, separator, relative = line.partition("  ")
        if not separator or len(digest) != 64:
            raise ThirdPartyIntegrityError("vendored checksum manifest is malformed")
        candidate = (checksums.parent / relative.removeprefix("./")).resolve()
        if not candidate.is_relative_to(checksums.parent.resolve()):
            raise ThirdPartyIntegrityError("vendored checksum path escapes its root")
        if not candidate.is_file() or candidate.is_symlink() or _sha256(candidate) != digest:
            raise ThirdPartyIntegrityError(f"vendored dependency changed: {relative}")
        checked += 1
    if checked == 0:
        raise ThirdPartyIntegrityError("vendored checksum manifest is empty")
    return ThirdPartyVerification(True, checked, source_names)
