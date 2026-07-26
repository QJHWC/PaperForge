from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path


class UnsafePathError(ValueError):
    """Raised when a writable path contains a symbolic-link component."""


def is_link_or_reparse_point(path: str | Path) -> bool:
    """Return true for POSIX links and Windows reparse-point redirects."""

    candidate = Path(path)
    try:
        details = candidate.lstat()
    except FileNotFoundError:
        return False
    attributes = int(getattr(details, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return candidate.is_symlink() or bool(attributes & reparse_flag)


def _absolute_lexical(path: str | Path) -> Path:
    lexical = Path(path).expanduser()
    if not lexical.is_absolute():
        lexical = Path.cwd() / lexical
    return lexical.absolute()


def reject_symlink_components(
    path: str | Path,
    *,
    anchor: str | Path | None = None,
) -> Path:
    lexical = _absolute_lexical(path)
    if anchor is None:
        current = lexical.parent
        if is_link_or_reparse_point(current):
            raise UnsafePathError(
                f"writable path contains a symbolic link: {current}"
            )
        anchor_path = current
    else:
        anchor_path = _absolute_lexical(anchor)
        if is_link_or_reparse_point(anchor_path):
            raise UnsafePathError(
                f"writable anchor is a symbolic link: {anchor_path}"
            )
        try:
            lexical.relative_to(anchor_path)
        except ValueError as exc:
            raise UnsafePathError("writable path leaves its trusted anchor") from exc
    current = anchor_path
    for part in lexical.relative_to(anchor_path).parts:
        current /= part
        if is_link_or_reparse_point(current):
            raise UnsafePathError(f"writable path contains a symbolic link: {current}")
    return lexical


def validate_writable_path(path: str | Path) -> Path:
    """Validate a future writable path without creating filesystem state."""

    lexical = _absolute_lexical(path)
    if is_link_or_reparse_point(lexical):
        raise UnsafePathError(
            f"writable path contains a symbolic link: {lexical}"
        )
    current = lexical.parent if lexical.exists() else lexical
    while not current.exists() and not is_link_or_reparse_point(current):
        if current.parent == current:
            break
        current = current.parent
    if is_link_or_reparse_point(current) or not current.is_dir():
        raise UnsafePathError(
            f"writable parent is not a safe directory: {current}"
        )
    reject_symlink_components(lexical, anchor=current)
    return lexical


def safe_mkdir(
    path: str | Path,
    *,
    anchor: str | Path | None = None,
) -> Path:
    lexical = _absolute_lexical(path)
    if anchor is None:
        current = lexical
        missing: list[Path] = []
        while not current.exists() and not is_link_or_reparse_point(current):
            missing.append(current)
            current = current.parent
        if is_link_or_reparse_point(current) or not current.is_dir():
            raise UnsafePathError(
                f"writable parent is not a safe directory: {current}"
            )
        anchor_path = current
    else:
        anchor_path = _absolute_lexical(anchor)
        if not anchor_path.exists() or not anchor_path.is_dir():
            raise UnsafePathError(
                f"writable anchor is not a safe directory: {anchor_path}"
            )
        reject_symlink_components(lexical, anchor=anchor_path)
        missing = []
        current = anchor_path
        for part in lexical.relative_to(anchor_path).parts:
            current /= part
            if is_link_or_reparse_point(current):
                raise UnsafePathError(
                    f"writable path contains a symbolic link: {current}"
                )
            if current.exists():
                if not current.is_dir():
                    raise UnsafePathError(
                        f"writable parent is not a safe directory: {current}"
                    )
            else:
                missing.append(current)
    for directory in reversed(missing) if anchor is None else missing:
        directory.mkdir()
        if is_link_or_reparse_point(directory) or not directory.is_dir():
            raise UnsafePathError(
                f"created writable path is not a safe directory: {directory}"
            )
    reject_symlink_components(lexical, anchor=anchor_path)
    return lexical.resolve(strict=True)


def safe_output_path(
    workspace: str | Path,
    relative_path: str | Path,
) -> Path:
    root = Path(workspace).expanduser().resolve(strict=True)
    relative = Path(relative_path)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise UnsafePathError("output path must be normalized and workspace-relative")
    parent = safe_mkdir(root / relative.parent, anchor=root)
    target = parent / relative.name
    reject_symlink_components(target, anchor=root)
    if is_link_or_reparse_point(target) or (
        target.exists() and not target.is_file()
    ):
        raise UnsafePathError(f"output target is not a regular file: {target}")
    resolved = target.resolve(strict=False)
    if root not in resolved.parents:
        raise UnsafePathError("output target leaves the workspace")
    return target


def atomic_write_text(
    workspace: str | Path,
    relative_path: str | Path,
    content: str,
    *,
    encoding: str = "utf-8",
) -> Path:
    target = safe_output_path(workspace, relative_path)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding=encoding,
            dir=target.parent,
            prefix=f".{target.name}.",
            delete=False,
        ) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            temporary = stream.name
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
    return target


__all__ = [
    "UnsafePathError",
    "atomic_write_text",
    "is_link_or_reparse_point",
    "reject_symlink_components",
    "safe_mkdir",
    "safe_output_path",
    "validate_writable_path",
]
