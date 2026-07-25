from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

REFERENCES_BIB = "references.bib"
_IGNORED_DIRS = {
    ".git",
    ".paperforge",
    ".pytest_cache",
    "__pycache__",
    "build",
    "dist",
    "out",
    "rendered-pages",
}


class BibliographyContractError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class BibliographyContract:
    path: Path
    uses_bibliography: bool
    citation_keys: tuple[str, ...]


def _safe_project_file(project_dir: Path, relative_path: str | Path) -> Path:
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise BibliographyContractError(f"unsafe project path: {relative_path}")
    resolved = (project_dir / relative).resolve()
    if not resolved.is_relative_to(project_dir):
        raise BibliographyContractError(f"path leaves publication project: {relative_path}")
    return resolved


def _iter_bib_files(project_dir: Path) -> tuple[Path, ...]:
    paths: list[Path] = []
    for path in project_dir.rglob("*.bib"):
        relative = path.relative_to(project_dir)
        if any(part in _IGNORED_DIRS or part.startswith(".") for part in relative.parts[:-1]):
            continue
        if path.is_symlink():
            raise BibliographyContractError(
                f"bibliography symlinks are not allowed: {relative.as_posix()}"
            )
        paths.append(path)
    return tuple(sorted(paths))


def _normalize_target(target: str) -> str:
    normalized = target.strip().replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    if not normalized.lower().endswith(".bib"):
        normalized += ".bib"
    return normalized


def _strip_tex_comments(tex_text: str) -> str:
    return re.sub(r"(?<!\\)%[^\n]*", "", tex_text)


def _extract_citation_keys(tex_text: str) -> tuple[str, ...]:
    keys: list[str] = []
    citation_pattern = re.compile(
        r"\\(?:[A-Za-z]*cite[A-Za-z]*|citeauthor|citeyear[A-Za-z]*)"
        r"\*?\s*(?:\[[^\]]*\]\s*)*\{([^}]*)\}",
        re.IGNORECASE,
    )
    for match in citation_pattern.finditer(tex_text):
        keys.extend(key.strip() for key in match.group(1).split(",") if key.strip())
    return tuple(keys)


def _extract_bibtex_keys(bib_text: str) -> tuple[str, ...]:
    return tuple(
        match.group(1).strip()
        for match in re.finditer(r"@\w+\s*\{\s*([^,\s]+)\s*,", bib_text)
    )


def validate_single_references_bib(
    project_dir: str | Path,
    main_tex: str | Path = "main.tex",
) -> BibliographyContract:
    project = Path(project_dir).expanduser().resolve()
    tex_path = _safe_project_file(project, main_tex)
    if not tex_path.is_file():
        raise BibliographyContractError(f"main TeX file is missing: {tex_path}")

    bibliography_path = project / REFERENCES_BIB
    if not bibliography_path.is_file():
        raise BibliographyContractError(
            f"publication projects require a single root-level {REFERENCES_BIB}"
        )
    if bibliography_path.is_symlink():
        raise BibliographyContractError(f"{REFERENCES_BIB} must not be a symlink")

    bib_files = _iter_bib_files(project)
    unexpected = [
        path.relative_to(project).as_posix()
        for path in bib_files
        if path.resolve() != bibliography_path.resolve()
    ]
    if unexpected:
        raise BibliographyContractError(
            f"only {REFERENCES_BIB} is allowed; remove or merge: {', '.join(unexpected)}"
        )

    tex_text = _strip_tex_comments(tex_path.read_text(encoding="utf-8"))
    embedded = re.findall(
        r"\\begin\{filecontents\*?\}\s*\{([^}]+\.bib)\}",
        tex_text,
        flags=re.IGNORECASE,
    )
    if embedded:
        raise BibliographyContractError(
            f"embedded bibliography files are forbidden; use {REFERENCES_BIB}"
        )

    bibliography_targets: list[str] = []
    for match in re.finditer(r"\\bibliography\s*\{([^}]*)\}", tex_text):
        bibliography_targets.extend(
            target for target in match.group(1).split(",") if target.strip()
        )
    for match in re.finditer(
        r"\\addbibresource(?:\s*\[[^\]]*\])?\s*\{([^}]*)\}",
        tex_text,
    ):
        bibliography_targets.append(match.group(1))

    normalized_targets = tuple(_normalize_target(target) for target in bibliography_targets)
    invalid_targets = sorted(
        {target for target in normalized_targets if target != REFERENCES_BIB}
    )
    if invalid_targets:
        raise BibliographyContractError(
            f"all bibliography directives must target {REFERENCES_BIB}; found "
            + ", ".join(invalid_targets)
        )
    if len(set(normalized_targets)) > 1:
        raise BibliographyContractError(
            f"publication projects may reference only {REFERENCES_BIB}"
        )

    citation_keys = _extract_citation_keys(tex_text)
    if citation_keys and not normalized_targets:
        raise BibliographyContractError(
            f"citations require a bibliography directive for {REFERENCES_BIB}"
        )

    bibtex_keys = _extract_bibtex_keys(bibliography_path.read_text(encoding="utf-8"))
    duplicate_keys = sorted({key for key in bibtex_keys if bibtex_keys.count(key) > 1})
    if duplicate_keys:
        raise BibliographyContractError(
            "duplicate BibTeX keys in references.bib: " + ", ".join(duplicate_keys)
        )

    return BibliographyContract(
        path=bibliography_path,
        uses_bibliography=bool(normalized_targets),
        citation_keys=citation_keys,
    )
