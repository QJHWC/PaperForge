from __future__ import annotations

import hashlib
import json
import os
import re
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

from engine.secret_redaction import (
    contains_secret,
    secret_values_from_env,
)
from paperforge.path_safety import safe_mkdir

_MAX_SOURCE_FILE_BYTES = 128 * 1024 * 1024
_MAX_SOURCE_BUNDLE_BYTES = 1024 * 1024 * 1024
_SECRET_FILENAME = re.compile(
    r"(?:^|[._-])(?:credential|identity|private|secret|token|key)"
    r"(?:[._-]|$)",
    re.IGNORECASE,
)
_SECRET_SCAN_INDICATORS = (
    b"sk-",
    b"sk_",
    b"akia",
    b"hf_",
    b"github_pat_",
    b"ghp_",
    b"gho_",
    b"ghu_",
    b"ghs_",
    b"ghr_",
    b"glpat-",
    b"xox",
    b"aiza",
    b"eyj",
    b"bearer",
    b"authorization",
    b"api_key",
    b"api-key",
    b"apikey",
    b"auth_token",
    b"auth-token",
    b"access_key",
    b"access-key",
    b"access_token",
    b"access-token",
    b"client_secret",
    b"client-secret",
    b"cookie",
    b"password",
    b"passphrase",
    b"secret",
    b"token",
    b"credential",
    b"private",
    b"://",
)


class SourceBundleError(ValueError):
    """Raised when an immutable compute source bundle cannot be verified."""


@dataclass(frozen=True)
class SourceBundle:
    path: Path
    archive_sha256: str
    source_sha256: str
    file_count: int
    size_bytes: int


def _sha256_stream(stream: object) -> tuple[str, int, bool]:
    digest = hashlib.sha256()
    size = 0
    overlap = b""
    has_secret = False
    configured_values = tuple(
        value.encode("utf-8")
        for value in secret_values_from_env(os.environ)
        if value
    )
    while True:
        chunk = stream.read(64 * 1024)  # type: ignore[attr-defined]
        if not chunk:
            break
        digest.update(chunk)
        size += len(chunk)
        scan = overlap + chunk
        lower_scan = scan.lower()
        requires_scan = any(
            marker in lower_scan
            for marker in _SECRET_SCAN_INDICATORS
        ) or any(value in scan for value in configured_values)
        if (
            requires_scan
            and contains_secret(scan)
            or b"PRIVATE KEY-----" in scan
        ):
            has_secret = True
        overlap = scan[-4096:]
    return digest.hexdigest(), size, has_secret


def _canonical_sha256(records: list[dict[str, object]]) -> str:
    payload = json.dumps(
        {"files": records},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def create_verified_source_bundle(
    source_snapshot: str | Path,
    *,
    canonical_worktree: str | Path,
    expected_source_sha256: str,
    staging_dir: str | Path,
) -> SourceBundle:
    lexical = Path(source_snapshot).expanduser()
    if lexical.is_symlink():
        raise SourceBundleError(
            "compute source snapshot must not be a symbolic link"
        )
    snapshot = lexical.resolve(strict=True)
    if not snapshot.is_dir():
        raise SourceBundleError(
            "compute source snapshot must be a directory"
        )
    stage = safe_mkdir(staging_dir)
    handle, raw_archive = tempfile.mkstemp(
        prefix="paperforge-source-",
        suffix=".tar",
        dir=stage,
    )
    os.close(handle)
    archive = Path(raw_archive)
    try:
        with tarfile.open(archive, mode="w", format=tarfile.PAX_FORMAT) as tar:
            total = 0
            for path in sorted(snapshot.rglob("*")):
                relative = path.relative_to(snapshot)
                if path.is_symlink():
                    raise SourceBundleError(
                        "compute source snapshot contains a symbolic link: "
                        f"{relative.as_posix()}"
                    )
                if not path.is_file():
                    continue
                if (
                    path.name == ".env"
                    or path.suffix.casefold()
                    in {".key", ".pem", ".p12", ".pfx"}
                    or _SECRET_FILENAME.search(path.name)
                ):
                    raise SourceBundleError(
                        "compute source bundle contains a denied path: "
                        f"{relative.as_posix()}"
                    )
                size = path.stat().st_size
                if size > _MAX_SOURCE_FILE_BYTES:
                    raise SourceBundleError(
                        "compute source file exceeds the source-bundle limit: "
                        f"{relative.as_posix()}"
                    )
                total += size
                if total > _MAX_SOURCE_BUNDLE_BYTES:
                    raise SourceBundleError(
                        "compute source snapshot exceeds the source-bundle limit"
                    )
                tar.add(
                    path,
                    arcname=relative.as_posix(),
                    recursive=False,
                )

        records: list[dict[str, object]] = []
        with tarfile.open(archive, mode="r:") as tar:
            for member in sorted(tar.getmembers(), key=lambda item: item.name):
                if not member.isfile():
                    raise SourceBundleError(
                        "compute source archive contains a non-file entry"
                    )
                stream = tar.extractfile(member)
                if stream is None:
                    raise SourceBundleError(
                        "compute source archive contains an unreadable file"
                    )
                digest, size, has_secret = _sha256_stream(stream)
                if contains_secret(member.name) or has_secret:
                    raise SourceBundleError(
                        "compute source bundle contains credential-like data: "
                        f"{member.name}"
                    )
                records.append(
                    {
                        "path": str(
                            Path(canonical_worktree) / Path(member.name)
                        ),
                        "sha256": digest,
                        "size_bytes": size,
                    }
                )
        source_sha256 = _canonical_sha256(records)
        if source_sha256 != expected_source_sha256:
            raise SourceBundleError(
                "compute source bundle does not match the approved snapshot"
            )
        with archive.open("rb") as stream:
            archive_sha256, archive_size, _ = _sha256_stream(stream)
        return SourceBundle(
            path=archive,
            archive_sha256=archive_sha256,
            source_sha256=source_sha256,
            file_count=len(records),
            size_bytes=archive_size,
        )
    except BaseException:
        archive.unlink(missing_ok=True)
        raise


__all__ = [
    "SourceBundle",
    "SourceBundleError",
    "create_verified_source_bundle",
]
