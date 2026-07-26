from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .models import ClaimStatus, ClaimType
from .scientific_memory import (
    NON_CLAIM_CATEGORIES,
    ScientificMemory,
    valid_non_claim_metadata,
)

_LEADING_STRUCTURE_COMMAND = re.compile(
    r"^\\(?:section|subsection|subsubsection|paragraph)\*?"
    r"(?:\s*\[[^\]]*\])?\s*\{[^{}]*\}\s*"
)
_LEADING_LABEL_COMMAND = re.compile(r"^\\label\s*\{[^{}]*\}\s*")
_LEADING_BIBLIOGRAPHY_COMMAND = re.compile(
    r"^\\(?:bibliographystyle|bibliography)\s*\{[^{}]*\}\s*"
)
_INCLUDE_COMMAND = re.compile(r"^\\(?:input|include)\s*\{([^{}]+)\}\s*$")
_ANY_INCLUDE_COMMAND = re.compile(r"\\(?:input|include)\b")
_NON_PROSE_COMMAND = re.compile(
    r"^\\(?:maketitle|tableofcontents|listoffigures|listoftables|"
    r"newpage|clearpage|appendix|noindent)\s*$"
)
_SENTENCE_BOUNDARY = re.compile(
    r"(?:(?<=[.!?])\s+(?=(?:\\[A-Za-z]+\{)*[A-Z])|"
    r"(?<=[。！？])(?:\s+|(?=[^\s])))"
)
_COMMENT = re.compile(r"(?<!\\)%.*$")


class ClaimManifestError(ValueError):
    pass


@dataclass(frozen=True)
class LatexClaimUnit:
    text: str
    file: str
    line_start: int
    line_end: int

    def span(self) -> dict[str, int | str]:
        return {
            "file": self.file,
            "start": self.line_start,
            "end": self.line_end,
        }


def _clean_claim_text(text: str) -> str:
    cleaned = re.sub(
        r"\\(?:begin|end)\{(?:abstract|itemize|enumerate|equation|"
        r"figure\*?|table\*?|tabular\*?|align\*?|gather\*?|"
        r"multline\*?|center|flushleft|flushright)\}",
        " ",
        text,
    )
    cleaned = re.sub(r"\\label\{[^}]+\}", " ", cleaned)
    cleaned = re.sub(r"\\(?:section|subsection|paragraph)\*?\{[^}]+\}", " ", cleaned)
    cleaned = re.sub(
        r"\\includegraphics(?:\s*\[[^\]]*\])?\s*\{[^{}]+\}",
        " ",
        cleaned,
    )
    cleaned = re.sub(
        r"\\caption(?:\s*\[[^\]]*\])?\s*\{([^{}]*)\}",
        r" \1 ",
        cleaned,
    )
    cleaned = re.sub(r"\\(?:centering|raggedright|raggedleft)\b", " ", cleaned)
    cleaned = re.sub(r"\\item\b", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def extract_latex_claim_units(
    tex_path: str | Path,
    *,
    relative_file: str | None = None,
) -> tuple[LatexClaimUnit, ...]:
    unresolved = Path(tex_path).expanduser()
    if unresolved.is_symlink():
        raise ClaimManifestError("main TeX file must not be a symbolic link")
    path = unresolved.resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    root = path.parent
    paragraphs: list[tuple[str, int, int, str]] = []
    visited: set[Path] = set()
    stack: list[Path] = []

    def collect(
        current: Path,
        *,
        require_document: bool,
        display_name: str,
    ) -> None:
        if current in stack:
            cycle = " -> ".join(item.name for item in (*stack, current))
            raise ClaimManifestError(f"cyclic TeX include: {cycle}")
        if current in visited:
            raise ClaimManifestError(
                f"TeX source is included more than once: {display_name}"
            )
        visited.add(current)
        stack.append(current)
        lines = current.read_text(encoding="utf-8").splitlines()
        in_document = not require_document
        saw_document = not require_document
        buffer: list[str] = []
        start_line = 0

        def flush(end_line: int) -> None:
            nonlocal buffer, start_line
            if not buffer:
                return
            paragraphs.append(
                (display_name, start_line, end_line, " ".join(buffer))
            )
            buffer = []
            start_line = 0

        for number, raw in enumerate(lines, start=1):
            line = _COMMENT.sub("", raw).strip()
            if line == r"\begin{document}":
                in_document = True
                saw_document = True
                continue
            if not in_document:
                continue
            bibliography = _LEADING_BIBLIOGRAPHY_COMMAND.match(line)
            if bibliography is not None:
                flush(number - 1)
                line = line[bibliography.end() :].strip()
                if not line:
                    continue
            if line == r"\end{document}":
                continue

            include_match = _INCLUDE_COMMAND.fullmatch(line)
            if include_match is not None:
                flush(number - 1)
                include_value = include_match.group(1).strip()
                candidate = current.parent / include_value
                if candidate.suffix == "":
                    candidate = candidate.with_suffix(".tex")
                if candidate.suffix.lower() != ".tex" or candidate.is_symlink():
                    raise ClaimManifestError(
                        f"unsafe TeX include: {include_value}"
                    )
                included = candidate.resolve()
                if root not in included.parents or not included.is_file():
                    raise ClaimManifestError(
                        f"TeX include leaves project or is missing: {include_value}"
                    )
                collect(
                    included,
                    require_document=False,
                    display_name=included.relative_to(root).as_posix(),
                )
                continue
            if _ANY_INCLUDE_COMMAND.search(line):
                raise ClaimManifestError(
                    "TeX include commands must appear alone on a line"
                )
            stripped_structure = False
            while True:
                structure = _LEADING_STRUCTURE_COMMAND.match(line)
                label = _LEADING_LABEL_COMMAND.match(line)
                match = structure or label
                if match is None:
                    break
                if not stripped_structure:
                    flush(number - 1)
                    stripped_structure = True
                line = line[match.end() :].strip()
            if not line or _NON_PROSE_COMMAND.fullmatch(line):
                flush(number - 1)
                continue
            if line in {
                r"\begin{abstract}",
                r"\end{abstract}",
                r"\begin{itemize}",
                r"\end{itemize}",
                r"\begin{enumerate}",
                r"\end{enumerate}",
                r"\begin{equation}",
                r"\end{equation}",
            }:
                flush(number - 1)
                continue
            if line.startswith(r"\item"):
                flush(number - 1)
            if not buffer:
                start_line = number
            buffer.append(line)
            if line.endswith((".", "?", "!", ";", "。", "？", "！", "；")):
                flush(number)
        flush(len(lines))
        stack.pop()
        if not saw_document:
            raise ClaimManifestError(
                f"main TeX file has no document environment: {display_name}"
            )

    collect(
        path,
        require_document=True,
        display_name=relative_file or path.name,
    )

    units: list[LatexClaimUnit] = []
    for file_name, line_start, line_end, paragraph in paragraphs:
        cleaned = _clean_claim_text(paragraph)
        if not cleaned or not any(character.isalpha() for character in cleaned):
            continue
        for sentence in _SENTENCE_BOUNDARY.split(cleaned):
            normalized = sentence.strip()
            if not normalized or not any(
                character.isalpha() for character in normalized
            ):
                continue
            units.append(
                LatexClaimUnit(
                    text=normalized,
                    file=file_name,
                    line_start=line_start,
                    line_end=line_end,
                )
            )
    return tuple(units)


EvidenceResolver = Callable[
    [LatexClaimUnit],
    tuple[ClaimType | str, ClaimStatus | str, Sequence[str]]
    | tuple[ClaimType | str, ClaimStatus | str, Sequence[str], str]
    | tuple[ClaimType | str, ClaimStatus | str, Sequence[str], str, str],
]


def import_latex_claims(
    memory: ScientificMemory,
    tex_path: str | Path,
    *,
    evidence_resolver: EvidenceResolver,
    relative_file: str | None = None,
    manifest_path: str | Path | None = None,
    non_claim_review_id: str | None = None,
) -> dict:
    units = extract_latex_claim_units(tex_path, relative_file=relative_file)
    spans: dict[str, dict[str, int | str]] = {}
    ordered_ids: list[str] = []
    for unit in units:
        resolution = evidence_resolver(unit)
        if len(resolution) == 3:
            claim_type, status, evidence_ids = resolution
            non_claim_category = ""
        elif len(resolution) == 4:
            claim_type, status, evidence_ids, non_claim_category = resolution
            resolved_non_claim_review_id = non_claim_review_id
        elif len(resolution) == 5:
            (
                claim_type,
                status,
                evidence_ids,
                non_claim_category,
                resolved_non_claim_review_id,
            ) = resolution
        else:
            raise ValueError("evidence resolver returned an invalid classification")
        if len(resolution) == 3:
            resolved_non_claim_review_id = None
        normalized_type = ClaimType(claim_type)
        normalized_status = ClaimStatus(status)
        evidence = tuple(dict.fromkeys(str(item) for item in evidence_ids))
        if normalized_type is ClaimType.NON_CLAIM:
            if (
                normalized_status is not ClaimStatus.NON_CLAIM
                or evidence
                or str(non_claim_category).strip() not in NON_CLAIM_CATEGORIES
                or not str(resolved_non_claim_review_id or "").strip()
            ):
                raise ValueError(
                    "NON_CLAIM requires status=NON_CLAIM, no evidence, "
                    "a controlled category, and a persisted approval review"
                )
        elif not evidence:
            raise ValueError(f"claim has no evidence mapping: {unit.text}")
        claim_id = memory.add_claim(
            claim_type=normalized_type,
            text=unit.text,
            status=normalized_status,
            metadata={
                "tex_span": unit.span(),
                **(
                    {
                        "non_claim_category": str(
                            non_claim_category
                        ).strip(),
                        "non_claim_review_id": str(
                            resolved_non_claim_review_id
                        ).strip(),
                    }
                    if normalized_type is ClaimType.NON_CLAIM
                    else {}
                ),
            },
        )
        for evidence_id in evidence:
            memory.link_claim(claim_id, evidence_id)
        spans[claim_id] = unit.span()
        ordered_ids.append(claim_id)

    manifest = memory.claim_manifest(spans)
    order = {claim_id: index for index, claim_id in enumerate(ordered_ids)}
    manifest["claims"].sort(key=lambda claim: order.get(claim["claim_id"], len(order)))
    manifest["coverage"] = {
        "latex_claim_units": len(units),
        "manifest_claims": len(manifest["claims"]),
        "mapped_claims": sum(
            bool(claim["evidence"])
            or (
                claim["claim_type"] == ClaimType.NON_CLAIM.value
                and isinstance(claim.get("metadata"), dict)
                and valid_non_claim_metadata(
                    str(claim.get("text", "")),
                    claim["metadata"],
                )
            )
            for claim in manifest["claims"]
        ),
        "percent": 100.0 if units and len(manifest["claims"]) == len(units) else 0.0,
    }
    if manifest_path is not None:
        destination = Path(manifest_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return manifest
