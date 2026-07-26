from __future__ import annotations

import hashlib
import shutil
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

from paperforge.path_safety import (
    is_link_or_reparse_point,
    reject_symlink_components,
    safe_mkdir,
    validate_writable_path,
)

from .contracts import ArtifactRecord


def _normalize_trusted_system_alias(path: Path) -> Path:
    """Canonicalize only immutable, platform-defined writable aliases."""

    lexical = path.expanduser().absolute()
    if sys.platform != "darwin":
        return lexical
    for alias, expected in (
        (Path("/var"), Path("/private/var")),
        (Path("/tmp"), Path("/private/tmp")),
    ):
        try:
            relative = lexical.relative_to(alias)
        except ValueError:
            continue
        if alias.is_symlink() and alias.resolve(strict=True) == expected:
            return expected.joinpath(relative)
    return lexical


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


def safe_artifact_root(
    path: str | Path,
    *,
    create: bool,
) -> Path:
    """Return a lexical artifact root without resolving through links."""

    lexical = _normalize_trusted_system_alias(Path(path))
    validate_writable_path(lexical)
    reject_symlink_components(lexical, anchor=Path(lexical.anchor))
    if create:
        return safe_mkdir(lexical)
    if is_link_or_reparse_point(lexical) or not lexical.is_dir():
        raise ValueError(f"artifact root is not a safe directory: {lexical}")
    reject_symlink_components(lexical, anchor=lexical)
    return lexical


def safe_artifact_destination(
    root: str | Path,
    relative_path: str | Path,
) -> Path:
    """Validate a future destination without creating any directories."""

    root_path = _normalize_trusted_system_alias(Path(root))
    validate_writable_path(root_path)
    reject_symlink_components(root_path, anchor=Path(root_path.anchor))
    relative = Path(relative_path)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ValueError("artifact path must be normalized and root-relative")
    target = root_path / relative
    reject_symlink_components(target, anchor=Path(target.anchor))
    if is_link_or_reparse_point(target) or (
        target.exists() and not target.is_file()
    ):
        raise ValueError(f"artifact target is not a regular file: {target}")
    return target


def safe_artifact_file(
    root: str | Path,
    relative_path: str | Path,
    *,
    require_exists: bool,
) -> Path:
    """Validate every lexical component of one artifact file path."""

    root_path = safe_artifact_root(root, create=not require_exists)
    relative = Path(relative_path)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ValueError("artifact path must be normalized and root-relative")
    target = root_path / relative
    if not require_exists:
        safe_mkdir(target.parent, anchor=root_path)
    reject_symlink_components(target, anchor=root_path)
    if is_link_or_reparse_point(target):
        raise ValueError(f"artifact path contains a symbolic link: {target}")
    if require_exists and not target.is_file():
        raise ValueError(f"artifact is not a regular file: {target}")
    if target.exists() and not target.is_file():
        raise ValueError(f"artifact target is not a regular file: {target}")
    return target


def file_record(
    path: Path,
    *,
    display_path: str | None = None,
    attempt_id: int | None = None,
) -> ArtifactRecord:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return ArtifactRecord(
        path=display_path or str(path),
        size_bytes=path.stat().st_size,
        sha256=digest.hexdigest(),
        attempt_id=attempt_id,
    )


def copy_local_artifacts(
    *,
    source_root: Path,
    destination_root: Path,
    patterns: Sequence[str],
    attempt_id: int | None = None,
) -> tuple[ArtifactRecord, ...]:
    source_root = source_root.expanduser().absolute()
    destination_root = destination_root.expanduser().absolute()
    if is_link_or_reparse_point(source_root):
        raise ValueError(f"artifact source root must not be a symbolic link: {source_root}")
    if is_link_or_reparse_point(destination_root):
        raise ValueError(
            f"artifact destination root must not be a symbolic link: {destination_root}"
        )
    if not source_root.is_dir():
        raise ValueError(f"artifact source root is not a directory: {source_root}")
    destination_root = safe_artifact_root(destination_root, create=True)
    resolved_source_root = source_root.resolve()
    resolved_destination_root = destination_root
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
                                attempt_id=attempt_id,
                            )
                        )
            elif source.is_file():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                records.append(
                    file_record(
                        destination,
                        display_path=str(destination.relative_to(resolved_destination_root)),
                        attempt_id=attempt_id,
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
