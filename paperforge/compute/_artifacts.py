from __future__ import annotations

import hashlib
import shutil
from collections.abc import Iterable, Sequence
from pathlib import Path

from .contracts import ArtifactRecord


def _assert_contained_regular_tree(path: Path, root: Path) -> None:
    if path.is_symlink():
        raise ValueError(f"artifact must not be a symbolic link: {path}")
    candidates = (path, *path.rglob("*")) if path.is_dir() else (path,)
    resolved_root = root.resolve()
    for candidate in candidates:
        if candidate.is_symlink():
            raise ValueError(f"artifact tree contains a symbolic link: {candidate}")
        try:
            candidate.resolve().relative_to(resolved_root)
        except ValueError as exc:
            raise ValueError(f"artifact escaped source root: {candidate}") from exc


def _assert_safe_destination(path: Path, root: Path) -> None:
    relative = path.relative_to(root)
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError(f"artifact destination contains a symbolic link: {current}")


def file_record(path: Path, *, display_path: str | None = None) -> ArtifactRecord:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return ArtifactRecord(
        path=display_path or str(path),
        size_bytes=path.stat().st_size,
        sha256=digest.hexdigest(),
    )


def copy_local_artifacts(
    *,
    source_root: Path,
    destination_root: Path,
    patterns: Sequence[str],
) -> tuple[ArtifactRecord, ...]:
    if source_root.is_symlink():
        raise ValueError(f"artifact source root must not be a symbolic link: {source_root}")
    if destination_root.is_symlink():
        raise ValueError(
            f"artifact destination root must not be a symbolic link: {destination_root}"
        )
    destination_root.mkdir(parents=True, exist_ok=True)
    resolved_source_root = source_root.resolve()
    resolved_destination_root = destination_root.resolve()
    records: list[ArtifactRecord] = []
    seen: set[Path] = set()
    for pattern in patterns:
        matches = sorted(source_root.glob(pattern))
        if not matches:
            raise FileNotFoundError(f"artifact pattern matched no files: {pattern}")
        for source in matches:
            resolved = source.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            _assert_contained_regular_tree(source, resolved_source_root)
            try:
                relative = source.relative_to(source_root)
            except ValueError as exc:
                raise ValueError(f"artifact escaped source root: {source}") from exc
            destination = resolved_destination_root / relative
            _assert_safe_destination(destination, resolved_destination_root)
            if source.is_dir():
                shutil.copytree(source, destination, dirs_exist_ok=True)
                for copied in sorted(destination.rglob("*")):
                    if copied.is_file():
                        records.append(
                            file_record(
                                copied,
                                display_path=str(copied.relative_to(resolved_destination_root)),
                            )
                        )
            elif source.is_file():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                records.append(
                    file_record(
                        destination,
                        display_path=str(destination.relative_to(resolved_destination_root)),
                    )
                )
    return tuple(records)


def artifact_patterns(
    declared: Iterable[str | Path],
    override: Sequence[str] | None,
) -> tuple[str, ...]:
    declared_patterns = tuple(str(pattern) for pattern in declared)
    patterns = (
        tuple(str(pattern) for pattern in override)
        if override is not None
        else declared_patterns
    )
    if not patterns:
        raise ValueError("no artifact paths were declared or requested")
    for pattern in patterns:
        path = Path(pattern)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"artifact pattern must stay within the job root: {pattern}")
    if override is not None:
        undeclared = sorted(set(patterns) - set(declared_patterns))
        if undeclared:
            raise ValueError(
                "artifact override must be a subset of declared outputs: "
                + ", ".join(undeclared)
            )
    return patterns
