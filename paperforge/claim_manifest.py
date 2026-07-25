from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .models import ClaimStatus, ClaimType
from .scientific_memory import ScientificMemory

_SECTION_COMMAND = re.compile(
    r"^\\(?:sub)*section\*?\{|^\\paragraph\{|^\\label\{|^\\bibliography"
)
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+(?=(?:\\[A-Za-z]+\{)*[A-Z])")
_COMMENT = re.compile(r"(?<!\\)%.*$")


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
    cleaned = re.sub(r"\\(?:begin|end)\{(?:abstract|itemize|enumerate|equation)\}", " ", text)
    cleaned = re.sub(r"\\label\{[^}]+\}", " ", cleaned)
    cleaned = re.sub(r"\\(?:section|subsection|paragraph)\*?\{[^}]+\}", " ", cleaned)
    cleaned = re.sub(r"\\item\b", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def extract_latex_claim_units(
    tex_path: str | Path,
    *,
    relative_file: str | None = None,
) -> tuple[LatexClaimUnit, ...]:
    path = Path(tex_path)
    lines = path.read_text(encoding="utf-8").splitlines()
    file_name = relative_file or path.name
    in_document = False
    in_bibliography = False
    paragraphs: list[tuple[int, int, str]] = []
    buffer: list[str] = []
    start_line = 0

    def flush(end_line: int) -> None:
        nonlocal buffer, start_line
        if not buffer:
            return
        paragraphs.append((start_line, end_line, " ".join(buffer)))
        buffer = []
        start_line = 0

    for number, raw in enumerate(lines, start=1):
        line = _COMMENT.sub("", raw).strip()
        if line == r"\begin{document}":
            in_document = True
            continue
        if not in_document:
            continue
        if line.startswith(r"\bibliographystyle") or line.startswith(r"\bibliography"):
            flush(number - 1)
            in_bibliography = True
        if in_bibliography or line == r"\end{document}":
            continue
        if not line or _SECTION_COMMAND.match(line):
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
        if line.endswith((".", "?", "!", ";")):
            flush(number)
    flush(len(lines))

    units: list[LatexClaimUnit] = []
    for line_start, line_end, paragraph in paragraphs:
        cleaned = _clean_claim_text(paragraph)
        if not cleaned or not re.search(r"[A-Za-z]", cleaned):
            continue
        for sentence in _SENTENCE_BOUNDARY.split(cleaned):
            normalized = sentence.strip()
            if not normalized or not re.search(r"[A-Za-z]", normalized):
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
    tuple[ClaimType | str, ClaimStatus | str, Sequence[str]],
]


def import_latex_claims(
    memory: ScientificMemory,
    tex_path: str | Path,
    *,
    evidence_resolver: EvidenceResolver,
    relative_file: str | None = None,
    manifest_path: str | Path | None = None,
) -> dict:
    units = extract_latex_claim_units(tex_path, relative_file=relative_file)
    spans: dict[str, dict[str, int | str]] = {}
    ordered_ids: list[str] = []
    for unit in units:
        claim_type, status, evidence_ids = evidence_resolver(unit)
        evidence = tuple(dict.fromkeys(str(item) for item in evidence_ids))
        if not evidence:
            raise ValueError(f"claim has no evidence mapping: {unit.text}")
        claim_id = memory.add_claim(
            claim_type=claim_type,
            text=unit.text,
            status=status,
            metadata={"tex_span": unit.span()},
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
        "mapped_claims": sum(bool(claim["evidence"]) for claim in manifest["claims"]),
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
