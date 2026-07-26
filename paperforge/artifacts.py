from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from engine.secret_redaction import redact_secrets, redact_structure

from .models import ArtifactTier, utc_now
from .scientific_memory import ScientificMemory, _stable_id

MANIFEST_SCHEMA_VERSION = 1

DEFAULT_ALLOWED_ROOTS = ("artifacts",)
DEFAULT_ALLOWED_SUFFIXES = (
    ".bib",
    ".ckpt",
    ".csv",
    ".html",
    ".jpeg",
    ".jpg",
    ".json",
    ".jsonl",
    ".log",
    ".md",
    ".npy",
    ".npz",
    ".onnx",
    ".parquet",
    ".pdf",
    ".png",
    ".pt",
    ".pth",
    ".safetensors",
    ".svg",
    ".tex",
    ".tsv",
    ".txt",
    ".yaml",
    ".yml",
)
DEFAULT_ALLOWED_KINDS = (
    "bibliography",
    "checkpoint",
    "code",
    "config",
    "data",
    "dataset",
    "evidence",
    "figure",
    "latex",
    "log",
    "manifest",
    "metrics",
    "paper",
    "release",
    "report",
    "result",
    "review",
    "source",
    "trace",
    "visualization",
)

_KIND_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]*$")


class ArtifactError(RuntimeError):
    """Base class for artifact storage failures."""


class ArtifactPathError(ArtifactError, ValueError):
    """Raised when a path is not a safe workspace-relative artifact path."""


class ArtifactNotAllowedError(ArtifactError, ValueError):
    """Raised when a path suffix, root, or artifact kind is not allowlisted."""


class ArtifactIntegrityError(ArtifactError):
    """Raised when an artifact or manifest no longer matches its digest."""


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(payload: Mapping[str, Any] | Sequence[Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _normalize_tier(tier: ArtifactTier | str | None) -> str | None:
    if tier is None:
        return None
    return ArtifactTier(tier).value


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    path: str
    kind: str
    sha256: str
    size_bytes: int
    created_at: str
    run_id: str | None = None
    tier: str | None = None
    status: str = "VERIFIED"
    media_type: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["metadata"] = dict(self.metadata)
        return payload


class ArtifactStore:
    """Content-addressed metadata over an allowlisted workspace subtree.

    The store never accepts absolute paths. Every read and write is resolved
    against ``workspace`` and checked again after symlink resolution.
    """

    def __init__(
        self,
        workspace: str | Path,
        *,
        allowed_roots: Iterable[str | Path] = DEFAULT_ALLOWED_ROOTS,
        allowed_suffixes: Iterable[str] = DEFAULT_ALLOWED_SUFFIXES,
        allowed_kinds: Iterable[str] = DEFAULT_ALLOWED_KINDS,
        path_allowlist: Iterable[str | Path] | None = None,
        memory: ScientificMemory | None = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.allowed_roots = tuple(
            self._normalize_policy_path(root, label="allowed root")
            for root in allowed_roots
        )
        if not self.allowed_roots:
            raise ValueError("at least one allowed root is required")
        self.allowed_suffixes = frozenset(self._normalize_suffix(value) for value in allowed_suffixes)
        self.allowed_kinds = frozenset(self._normalize_kind(value) for value in allowed_kinds)
        if not self.allowed_suffixes:
            raise ValueError("at least one allowed suffix is required")
        if not self.allowed_kinds:
            raise ValueError("at least one allowed kind is required")
        self.path_allowlist = (
            tuple(
                self._normalize_policy_path(path, label="allowlisted path")
                for path in path_allowlist
            )
            if path_allowlist is not None
            else None
        )
        self.memory = memory
        self._records: dict[str, ArtifactRecord] = {}

    @staticmethod
    def _normalize_suffix(value: str) -> str:
        suffix = str(value).strip().lower()
        if not suffix:
            return ""
        if not suffix.startswith(".") or "/" in suffix or "\\" in suffix:
            raise ValueError(f"invalid allowed suffix: {value!r}")
        return suffix

    @staticmethod
    def _normalize_kind(value: str) -> str:
        kind = str(value).strip().lower()
        if not _KIND_PATTERN.fullmatch(kind):
            raise ValueError(f"invalid artifact kind: {value!r}")
        return kind

    @staticmethod
    def _normalize_policy_path(value: str | Path, *, label: str) -> str:
        raw = os.fspath(value)
        if not raw or "\x00" in raw or "\\" in raw:
            raise ArtifactPathError(f"invalid {label}: {raw!r}")
        path = Path(raw)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise ArtifactPathError(f"{label} must be a normalized relative path: {raw!r}")
        return path.as_posix().rstrip("/")

    def _normalize_relative_path(self, value: str | Path) -> str:
        raw = os.fspath(value)
        if not raw or "\x00" in raw or "\\" in raw:
            raise ArtifactPathError(f"invalid artifact path: {raw!r}")
        path = Path(raw)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise ArtifactPathError(f"artifact path must be workspace-relative: {raw!r}")
        normalized = path.as_posix()
        if normalized in {"", "."}:
            raise ArtifactPathError("artifact path must name a file")
        return normalized

    @staticmethod
    def _is_within(relative_path: str, parent: str) -> bool:
        return relative_path == parent or relative_path.startswith(f"{parent}/")

    def _has_symlink_component(self, relative_path: str) -> bool:
        candidate = self.workspace
        for part in Path(relative_path).parts:
            candidate = candidate / part
            if candidate.is_symlink():
                return True
            if not candidate.exists():
                break
        return False

    def _validate_allowlist(self, relative_path: str, *, kind: str | None = None) -> None:
        if not any(self._is_within(relative_path, root) for root in self.allowed_roots):
            raise ArtifactNotAllowedError(
                f"artifact path is outside allowed roots: {relative_path}"
            )
        if self.path_allowlist is not None and not any(
            relative_path == allowed
            or (not Path(allowed).suffix and self._is_within(relative_path, allowed))
            for allowed in self.path_allowlist
        ):
            raise ArtifactNotAllowedError(f"artifact path is not allowlisted: {relative_path}")
        suffix = Path(relative_path).suffix.lower()
        if suffix not in self.allowed_suffixes:
            raise ArtifactNotAllowedError(
                f"artifact suffix is not allowlisted: {suffix or '<none>'}"
            )
        if kind is not None and self._normalize_kind(kind) not in self.allowed_kinds:
            raise ArtifactNotAllowedError(f"artifact kind is not allowlisted: {kind}")

    def resolve(self, relative_path: str | Path, *, kind: str | None = None) -> Path:
        normalized = self._normalize_relative_path(relative_path)
        self._validate_allowlist(normalized, kind=kind)
        if self._has_symlink_component(normalized):
            raise ArtifactPathError(
                f"artifact path contains a symlink component: {normalized}"
            )
        resolved = (self.workspace / normalized).resolve(strict=False)
        if resolved == self.workspace or self.workspace not in resolved.parents:
            raise ArtifactPathError(f"artifact path escapes workspace: {normalized}")
        return resolved

    def relative_path(self, path: str | Path) -> str:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            normalized = self._normalize_relative_path(candidate)
            self.resolve(normalized)
            return normalized
        resolved = candidate.resolve(strict=False)
        try:
            relative = resolved.relative_to(self.workspace).as_posix()
        except ValueError as exc:
            raise ArtifactPathError(f"path is outside workspace: {candidate}") from exc
        # Absolute paths may be converted for reporting, but never accepted by
        # write/register APIs.
        self._validate_allowlist(relative)
        return relative

    def _record(
        self,
        relative_path: str,
        *,
        kind: str,
        run_id: str | None,
        tier: ArtifactTier | str | None,
        status: str,
        media_type: str | None,
        metadata: Mapping[str, Any] | None,
        track: bool,
    ) -> ArtifactRecord:
        path = self.resolve(relative_path, kind=kind)
        if path.is_symlink() or not path.is_file():
            raise ArtifactPathError(f"artifact must be a regular file: {relative_path}")
        normalized_kind = self._normalize_kind(kind)
        digest = sha256_file(path)
        normalized_tier = _normalize_tier(tier)
        safe_metadata = redact_structure(dict(metadata or {}))
        record = ArtifactRecord(
            artifact_id=_stable_id("artifact", relative_path, digest, normalized_kind),
            path=relative_path,
            kind=normalized_kind,
            sha256=digest,
            size_bytes=path.stat().st_size,
            created_at=utc_now(),
            run_id=run_id,
            tier=normalized_tier,
            status=str(status),
            media_type=media_type,
            metadata=safe_metadata,
        )
        if track:
            self._records[relative_path] = record
        if self.memory is not None:
            with self.memory.connect() as db:
                db.execute(
                    """
                    INSERT OR REPLACE INTO artifacts
                    (id, run_id, kind, path, sha256, tier, status, created_at, metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.artifact_id,
                        run_id,
                        normalized_kind,
                        relative_path,
                        digest,
                        normalized_tier,
                        record.status,
                        record.created_at,
                        json.dumps(
                            {
                                **dict(record.metadata),
                                "media_type": media_type,
                                "size_bytes": record.size_bytes,
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    ),
                )
        return record

    def register(
        self,
        relative_path: str | Path,
        *,
        kind: str,
        run_id: str | None = None,
        tier: ArtifactTier | str | None = None,
        status: str = "VERIFIED",
        media_type: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ArtifactRecord:
        normalized = self._normalize_relative_path(relative_path)
        self.resolve(normalized, kind=kind)
        return self._record(
            normalized,
            kind=kind,
            run_id=run_id,
            tier=tier,
            status=status,
            media_type=media_type,
            metadata=metadata,
            track=True,
        )

    add = register

    def write_bytes(
        self,
        relative_path: str | Path,
        content: bytes,
        *,
        kind: str,
        run_id: str | None = None,
        tier: ArtifactTier | str | None = None,
        status: str = "VERIFIED",
        media_type: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        _track: bool = True,
    ) -> ArtifactRecord:
        normalized = self._normalize_relative_path(relative_path)
        target = self.resolve(normalized, kind=kind)
        target.parent.mkdir(parents=True, exist_ok=True)
        target = self.resolve(normalized, kind=kind)
        if target.exists() and target.is_symlink():
            raise ArtifactPathError(f"refusing to replace symlink artifact: {normalized}")
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=target.parent,
                prefix=f".{target.name}.",
                delete=False,
            ) as handle:
                handle.write(bytes(content))
                handle.flush()
                os.fsync(handle.fileno())
                temporary_name = handle.name
            os.replace(temporary_name, target)
            temporary_name = None
        finally:
            if temporary_name is not None:
                Path(temporary_name).unlink(missing_ok=True)
        return self._record(
            normalized,
            kind=kind,
            run_id=run_id,
            tier=tier,
            status=status,
            media_type=media_type,
            metadata=metadata,
            track=_track,
        )

    def write_text(
        self,
        relative_path: str | Path,
        content: str,
        *,
        kind: str,
        encoding: str = "utf-8",
        **kwargs: Any,
    ) -> ArtifactRecord:
        safe_content = redact_secrets(content)
        return self.write_bytes(
            relative_path,
            safe_content.encode(encoding),
            kind=kind,
            **kwargs,
        )

    def write_json(
        self,
        relative_path: str | Path,
        payload: Mapping[str, Any] | Sequence[Any],
        *,
        kind: str,
        indent: int = 2,
        **kwargs: Any,
    ) -> ArtifactRecord:
        rendered = json.dumps(
            redact_structure(payload),
            ensure_ascii=False,
            indent=indent,
            sort_keys=True,
        )
        return self.write_text(
            relative_path,
            f"{rendered}\n",
            kind=kind,
            media_type="application/json",
            **kwargs,
        )

    @property
    def records(self) -> tuple[ArtifactRecord, ...]:
        return tuple(self._records[path] for path in sorted(self._records))

    def build_manifest(
        self,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "workspace": ".",
            "generated_at": utc_now(),
            "artifacts": [record.to_dict() for record in self.records],
            "metadata": redact_structure(dict(metadata or {})),
        }
        payload["manifest_sha256"] = sha256_bytes(_canonical_json(payload))
        return payload

    manifest = build_manifest
    create_manifest = build_manifest

    def write_manifest(
        self,
        relative_path: str | Path = "artifacts/manifest.json",
        *,
        kind: str = "manifest",
        metadata: Mapping[str, Any] | None = None,
    ) -> ArtifactRecord:
        payload = self.build_manifest(metadata=metadata)
        return self.write_json(
            relative_path,
            payload,
            kind=kind,
            metadata={"manifest_sha256": payload["manifest_sha256"]},
            _track=False,
        )

    save_manifest = write_manifest

    def _load_manifest(
        self,
        manifest: Mapping[str, Any] | str | Path,
    ) -> dict[str, Any]:
        if isinstance(manifest, Mapping):
            return dict(manifest)
        normalized = self._normalize_relative_path(manifest)
        path = self.resolve(normalized)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ArtifactIntegrityError(f"invalid artifact manifest: {normalized}") from exc
        if not isinstance(payload, dict):
            raise ArtifactIntegrityError("artifact manifest root must be an object")
        return payload

    def verify_manifest(
        self,
        manifest: Mapping[str, Any] | str | Path,
    ) -> dict[str, Any]:
        payload = self._load_manifest(manifest)
        if payload.get("schema_version") != MANIFEST_SCHEMA_VERSION:
            raise ArtifactIntegrityError("unsupported artifact manifest schema")
        claimed_manifest_hash = payload.get("manifest_sha256")
        unsigned = dict(payload)
        unsigned.pop("manifest_sha256", None)
        actual_manifest_hash = sha256_bytes(_canonical_json(unsigned))
        if claimed_manifest_hash != actual_manifest_hash:
            raise ArtifactIntegrityError("artifact manifest digest mismatch")
        artifacts = payload.get("artifacts")
        if not isinstance(artifacts, list):
            raise ArtifactIntegrityError("artifact manifest artifacts must be a list")
        verified: list[str] = []
        seen: set[str] = set()
        for item in artifacts:
            if not isinstance(item, Mapping):
                raise ArtifactIntegrityError("artifact manifest entry must be an object")
            relative_path = item.get("path")
            expected_hash = item.get("sha256")
            expected_size = item.get("size_bytes")
            kind = item.get("kind")
            if not all(
                (
                    isinstance(relative_path, str),
                    isinstance(expected_hash, str),
                    isinstance(expected_size, int),
                    isinstance(kind, str),
                )
            ):
                raise ArtifactIntegrityError("artifact manifest entry is incomplete")
            assert isinstance(relative_path, str)
            assert isinstance(expected_hash, str)
            assert isinstance(expected_size, int)
            assert isinstance(kind, str)
            if relative_path in seen:
                raise ArtifactIntegrityError(f"duplicate artifact manifest path: {relative_path}")
            seen.add(relative_path)
            path = self.resolve(relative_path, kind=kind)
            if path.is_symlink() or not path.is_file():
                raise ArtifactIntegrityError(f"artifact is missing: {relative_path}")
            if path.stat().st_size != expected_size or sha256_file(path) != expected_hash:
                raise ArtifactIntegrityError(f"artifact digest mismatch: {relative_path}")
            verified.append(relative_path)
        return {
            "verified": True,
            "artifact_count": len(verified),
            "paths": verified,
            "manifest_sha256": actual_manifest_hash,
        }

    verify = verify_manifest


__all__ = [
    "ArtifactError",
    "ArtifactIntegrityError",
    "ArtifactNotAllowedError",
    "ArtifactPathError",
    "ArtifactRecord",
    "ArtifactStore",
    "DEFAULT_ALLOWED_KINDS",
    "DEFAULT_ALLOWED_ROOTS",
    "DEFAULT_ALLOWED_SUFFIXES",
    "MANIFEST_SCHEMA_VERSION",
    "sha256_bytes",
    "sha256_file",
]
