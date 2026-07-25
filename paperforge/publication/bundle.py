from __future__ import annotations

import contextlib
import hashlib
import json
import os
import zipfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .bibliography import REFERENCES_BIB, validate_single_references_bib

SOURCE_LOCK_SCHEMA = "paperforge.publication.source-lock/v1"
SOURCE_BUNDLE_SCHEMA = "paperforge.publication.source-bundle/v1"

DEFAULT_SOURCE_SUFFIXES = frozenset(
    {
        ".bbx",
        ".bib",
        ".bst",
        ".cbx",
        ".cfg",
        ".clo",
        ".cls",
        ".csv",
        ".def",
        ".enc",
        ".eps",
        ".fd",
        ".jpeg",
        ".jpg",
        ".lbx",
        ".map",
        ".otf",
        ".pdf",
        ".png",
        ".sty",
        ".svg",
        ".tex",
        ".tsv",
        ".ttf",
    }
)
DEFAULT_EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".paperforge",
        ".pytest_cache",
        "__pycache__",
        "build",
        "checkpoints",
        "dist",
        "out",
        "rendered-pages",
    }
)


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_json(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            indent=2,
            separators=(",", ": "),
        )
        + "\n"
    ).encode("utf-8")


def _safe_relative_path(value: str) -> Path:
    posix = PurePosixPath(value)
    if posix.is_absolute() or ".." in posix.parts or not posix.parts:
        raise ValueError(f"unsafe source-lock path: {value}")
    return Path(*posix.parts)


@dataclass(frozen=True, slots=True)
class BundleResult:
    bundle_path: Path
    checksum_path: Path
    source_lock_path: Path
    sha256: str
    source_lock_sha256: str
    files: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LockMismatch:
    path: str
    expected: str | None
    actual: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class SourceLockVerification:
    valid: bool
    mismatches: tuple[LockMismatch, ...]
    checked_files: int


class SourceBundler:
    def __init__(
        self,
        *,
        allowed_suffixes: Iterable[str] = DEFAULT_SOURCE_SUFFIXES,
        excluded_dirs: Iterable[str] = DEFAULT_EXCLUDED_DIRS,
    ) -> None:
        self.allowed_suffixes = frozenset(
            suffix.lower() if suffix.startswith(".") else f".{suffix.lower()}"
            for suffix in allowed_suffixes
        )
        self.excluded_dirs = frozenset(excluded_dirs)

    def collect(
        self,
        project_dir: str | Path,
        *,
        main_tex: str | Path = "main.tex",
        validate_bibliography: bool = True,
        excluded_paths: Iterable[str | Path] = (),
    ) -> tuple[Path, ...]:
        project = Path(project_dir).expanduser().resolve()
        main_relative = Path(main_tex)
        if main_relative.is_absolute() or ".." in main_relative.parts:
            raise ValueError(f"unsafe main TeX path: {main_tex}")
        main_path = (project / main_relative).resolve()
        if not main_path.is_relative_to(project) or not main_path.is_file():
            raise FileNotFoundError(main_path)
        if validate_bibliography:
            validate_single_references_bib(project, main_relative)

        excluded_resolved = {main_path.with_suffix(".pdf")}
        for excluded_value in excluded_paths:
            excluded = Path(excluded_value).expanduser()
            if not excluded.is_absolute():
                excluded = project / excluded
            excluded_resolved.add(excluded.resolve())
        files: list[Path] = []
        for path in project.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(project)
            if any(
                part in self.excluded_dirs or part.startswith(".")
                for part in relative.parts
            ):
                continue
            if path.is_symlink():
                raise ValueError(
                    f"source bundle does not allow symlinks: {relative.as_posix()}"
                )
            if path.resolve() in excluded_resolved:
                continue
            if path.suffix.lower() not in self.allowed_suffixes:
                continue
            files.append(path)

        required = {main_path, project / REFERENCES_BIB}
        missing = [path for path in required if path not in files]
        if missing:
            raise ValueError(
                "required publication sources are not allowlisted: "
                + ", ".join(str(path) for path in missing)
            )
        return tuple(sorted(files, key=lambda path: path.relative_to(project).as_posix()))

    def build(
        self,
        project_dir: str | Path,
        output_path: str | Path,
        *,
        profile: str | Any = "generic",
        main_tex: str | Path = "main.tex",
        source_lock_path: str | Path | None = None,
        excluded_paths: Iterable[str | Path] = (),
    ) -> BundleResult:
        project = Path(project_dir).expanduser().resolve()
        output = Path(output_path).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        excluded_values = tuple(excluded_paths)
        files = self.collect(
            project,
            main_tex=main_tex,
            excluded_paths=excluded_values,
        )
        profile_name = str(getattr(profile, "name", profile))
        relative_main = Path(main_tex).as_posix()
        excluded_relative: list[str] = []
        for excluded_value in excluded_values:
            excluded = Path(excluded_value).expanduser()
            if not excluded.is_absolute():
                excluded = project / excluded
            try:
                excluded_relative.append(
                    excluded.resolve().relative_to(project).as_posix()
                )
            except ValueError:
                continue

        file_records = []
        archive_payloads: dict[str, bytes] = {}
        for path in files:
            relative = path.relative_to(project).as_posix()
            content = path.read_bytes()
            digest = _sha256_bytes(content)
            file_records.append(
                {
                    "path": relative,
                    "sha256": digest,
                    "size": len(content),
                }
            )
            archive_payloads[relative] = content

        lock_payload = {
            "schema": SOURCE_LOCK_SCHEMA,
            "bundle_schema": SOURCE_BUNDLE_SCHEMA,
            "profile": profile_name,
            "entrypoint": relative_main,
            "bibliography": REFERENCES_BIB,
            "dependency_policy": {
                "floating_references_allowed": False,
                "mode": "vendored",
                "network_required": False,
            },
            "excluded_generated_paths": sorted(set(excluded_relative)),
            "files": file_records,
        }
        lock_bytes = _stable_json(lock_payload)
        lock_digest = _sha256_bytes(lock_bytes)
        archive_payloads["publication.source.lock.json"] = lock_bytes

        checksum_lines = [
            f"{record['sha256']}  {record['path']}" for record in file_records
        ]
        checksum_lines.append(
            f"{lock_digest}  publication.source.lock.json"
        )
        archive_payloads["SHA256SUMS"] = (
            "\n".join(sorted(checksum_lines)) + "\n"
        ).encode("ascii")

        temporary = output.with_name(f".{output.name}.tmp.{os.getpid()}")
        try:
            with zipfile.ZipFile(
                temporary,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            ) as archive:
                for relative in sorted(archive_payloads):
                    info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
                    info.create_system = 3
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = (0o100644 & 0xFFFF) << 16
                    archive.writestr(
                        info,
                        archive_payloads[relative],
                        compress_type=zipfile.ZIP_DEFLATED,
                        compresslevel=9,
                    )
            os.replace(temporary, output)
        finally:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()

        archive_digest = _sha256_file(output)
        checksum_path = output.with_suffix(output.suffix + ".sha256")
        checksum_path.write_text(
            f"{archive_digest}  {output.name}\n",
            encoding="ascii",
        )
        lock_output = (
            Path(source_lock_path).expanduser().resolve()
            if source_lock_path is not None
            else output.parent / "publication.source.lock.json"
        )
        lock_output.parent.mkdir(parents=True, exist_ok=True)
        lock_output.write_bytes(lock_bytes)
        return BundleResult(
            bundle_path=output,
            checksum_path=checksum_path,
            source_lock_path=lock_output,
            sha256=archive_digest,
            source_lock_sha256=lock_digest,
            files=tuple(str(record["path"]) for record in file_records),
        )


def verify_source_lock(
    project_dir: str | Path,
    source_lock_path: str | Path,
) -> SourceLockVerification:
    project = Path(project_dir).expanduser().resolve()
    lock_path = Path(source_lock_path).expanduser().resolve()
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return SourceLockVerification(
            valid=False,
            mismatches=(
                LockMismatch(
                    "<contract>",
                    "valid JSON source lock",
                    None,
                    str(exc),
                ),
            ),
            checked_files=0,
        )
    if not isinstance(payload, dict):
        return SourceLockVerification(
            valid=False,
            mismatches=(
                LockMismatch(
                    "<contract>",
                    "JSON object",
                    type(payload).__name__,
                    "invalid source-lock root",
                ),
            ),
            checked_files=0,
        )
    mismatches: list[LockMismatch] = []

    if payload.get("schema") != SOURCE_LOCK_SCHEMA:
        mismatches.append(
            LockMismatch(
                path="<contract>",
                expected=SOURCE_LOCK_SCHEMA,
                actual=str(payload.get("schema")),
                reason="schema mismatch",
            )
        )
    if payload.get("bundle_schema") != SOURCE_BUNDLE_SCHEMA:
        mismatches.append(
            LockMismatch(
                path="<bundle_schema>",
                expected=SOURCE_BUNDLE_SCHEMA,
                actual=str(payload.get("bundle_schema")),
                reason="bundle schema mismatch",
            )
        )
    if payload.get("bibliography") != REFERENCES_BIB:
        mismatches.append(
            LockMismatch(
                path="<bibliography>",
                expected=REFERENCES_BIB,
                actual=str(payload.get("bibliography")),
                reason="single bibliography contract mismatch",
            )
        )
    expected_policy = {
        "floating_references_allowed": False,
        "mode": "vendored",
        "network_required": False,
    }
    if payload.get("dependency_policy") != expected_policy:
        mismatches.append(
            LockMismatch(
                path="<dependency_policy>",
                expected=json.dumps(expected_policy, sort_keys=True),
                actual=json.dumps(payload.get("dependency_policy"), sort_keys=True),
                reason="publication sources must be vendored and offline",
            )
        )

    records = payload.get("files")
    if not isinstance(records, list):
        records = []
        mismatches.append(
            LockMismatch(
                path="<files>",
                expected="list",
                actual=type(payload.get("files")).__name__,
                reason="invalid source-lock files field",
            )
        )

    expected_paths: set[str] = set()
    checked = 0
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            mismatches.append(
                LockMismatch("<files>", "file record", repr(record), "invalid record")
            )
            continue
        relative_text = record["path"]
        try:
            relative = _safe_relative_path(relative_text)
        except ValueError as exc:
            mismatches.append(
                LockMismatch(relative_text, None, None, str(exc))
            )
            continue
        normalized_relative = PurePosixPath(*relative.parts).as_posix()
        if normalized_relative in expected_paths:
            mismatches.append(
                LockMismatch(
                    normalized_relative,
                    str(record.get("sha256")),
                    None,
                    "duplicate source-lock path",
                )
            )
            continue
        expected_paths.add(normalized_relative)
        source = (project / relative).resolve()
        if not source.is_relative_to(project):
            mismatches.append(
                LockMismatch(relative_text, record.get("sha256"), None, "path escapes project")
            )
            continue
        if not source.is_file() or source.is_symlink():
            mismatches.append(
                LockMismatch(relative_text, record.get("sha256"), None, "source missing")
            )
            continue
        checked += 1
        actual = _sha256_file(source)
        expected = str(record.get("sha256"))
        if actual != expected:
            mismatches.append(
                LockMismatch(relative_text, expected, actual, "sha256 mismatch")
            )
        expected_size = record.get("size")
        if not isinstance(expected_size, int) or source.stat().st_size != expected_size:
            mismatches.append(
                LockMismatch(
                    relative_text,
                    str(expected_size),
                    str(source.stat().st_size),
                    "size mismatch",
                )
            )

    raw_excluded_paths = payload.get("excluded_generated_paths", ())
    if not isinstance(raw_excluded_paths, list) or not all(
        isinstance(item, str) for item in raw_excluded_paths
    ):
        mismatches.append(
            LockMismatch(
                "<excluded_generated_paths>",
                "list[str]",
                type(raw_excluded_paths).__name__,
                "invalid excluded path list",
            )
        )
        raw_excluded_paths = []
    try:
        actual_files = SourceBundler().collect(
            project,
            main_tex=str(payload.get("entrypoint", "main.tex")),
            validate_bibliography=False,
            excluded_paths=tuple(raw_excluded_paths),
        )
        actual_paths = {
            path.relative_to(project).as_posix() for path in actual_files
        }
        for unexpected in sorted(actual_paths - expected_paths):
            mismatches.append(
                LockMismatch(unexpected, None, _sha256_file(project / unexpected), "unexpected source")
            )
    except (FileNotFoundError, ValueError) as exc:
        mismatches.append(
            LockMismatch("<source-set>", None, None, str(exc))
        )

    mismatches.sort(key=lambda item: (item.path, item.reason))
    return SourceLockVerification(
        valid=not mismatches,
        mismatches=tuple(mismatches),
        checked_files=checked,
    )


def verify_bundle_checksum(
    bundle_path: str | Path,
    checksum_path: str | Path | None = None,
) -> bool:
    bundle = Path(bundle_path).expanduser().resolve()
    sidecar = (
        Path(checksum_path).expanduser().resolve()
        if checksum_path is not None
        else bundle.with_suffix(bundle.suffix + ".sha256")
    )
    try:
        parts = sidecar.read_text(encoding="ascii").split()
        expected = parts[0]
        return len(expected) == 64 and _sha256_file(bundle) == expected
    except (OSError, UnicodeError, IndexError):
        return False
