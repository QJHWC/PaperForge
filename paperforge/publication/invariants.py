from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from paperforge.protected_blocks import (
    ProtectedBlockViolation,
    extract_protected_blocks,
)


class PublicationInvariantViolation(RuntimeError):
    pass


_CITATION_PATTERN = re.compile(
    r"\\(?:[A-Za-z]*cite[A-Za-z]*|citeauthor|citeyear[A-Za-z]*)"
    r"\*?\s*(?:\[[^\]]*\]\s*)*\{([^}]*)\}",
    re.IGNORECASE,
)
_NUMBER_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_.])"
    r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?"
    r"(?:\\?%|\\?[A-Za-z]+)?"
    r"(?![A-Za-z0-9_.])"
)
_LAYOUT_PATTERNS = (
    re.compile(
        r"\\(?:setlength|addtolength)\s*\{[^{}]*\}\s*\{[^{}]*\}",
        re.MULTILINE,
    ),
    re.compile(
        r"\\renewcommand\s*\{\s*\\arraystretch\s*\}\s*\{[^{}]*\}",
        re.MULTILINE,
    ),
    re.compile(
        r"\\(?:vspace|hspace|enlargethispage)\*?\s*\{[^{}]*\}",
        re.MULTILINE,
    ),
    re.compile(r"\\(?:raggedbottom|flushbottom|sloppy|fussy)\b", re.MULTILINE),
)
_CLAIM_MACRO_PATTERN = re.compile(
    r"\\(?:paperforgeclaim|PFClaim)\s*\{([^{}]*)\}",
    re.IGNORECASE | re.DOTALL,
)
_CLAIM_BLOCK_PATTERN = re.compile(
    r"%\s*PAPERFORGE-CLAIM-START(?::[^\n]*)?\n"
    r"(.*?)"
    r"%\s*PAPERFORGE-CLAIM-END(?::[^\n]*)?",
    re.IGNORECASE | re.DOTALL,
)


def _strip_layout(text: str) -> str:
    stripped = text
    for pattern in _LAYOUT_PATTERNS:
        stripped = pattern.sub("", stripped)
    stripped = re.sub(
        r"\\includegraphics(?:\s*\[[^\]]*\])?",
        r"\\includegraphics",
        stripped,
        flags=re.MULTILINE,
    )
    return stripped


def _strip_comments(text: str) -> str:
    return re.sub(r"(?<!\\)%[^\n]*", "", text)


def _citations(text: str) -> tuple[str, ...]:
    keys: list[str] = []
    for match in _CITATION_PATTERN.finditer(text):
        keys.extend(key.strip() for key in match.group(1).split(",") if key.strip())
    return tuple(keys)


def _semantic_text(text: str) -> str:
    normalized = _strip_layout(text)
    normalized = _CITATION_PATTERN.sub(r"\\PFCITATION{}", normalized)
    normalized = _strip_comments(normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _content_numbers(text: str) -> tuple[str, ...]:
    normalized = _strip_layout(text)
    normalized = _CITATION_PATTERN.sub("", normalized)
    normalized = _strip_comments(normalized)
    return tuple(match.group(0) for match in _NUMBER_PATTERN.finditer(normalized))


def _claim_texts_from_memory(
    scientific_memory: Any | None,
    claim_spans: Mapping[str, Mapping[str, Any]] | None,
) -> tuple[str, ...]:
    if scientific_memory is None:
        return ()
    manifest = scientific_memory.claim_manifest(claim_spans or {})
    claims = manifest.get("claims", ())
    return tuple(
        str(claim.get("text", "")).strip()
        for claim in claims
        if str(claim.get("text", "")).strip()
    )


def _claim_span_segments(
    text: str,
    claim_spans: Mapping[str, Mapping[str, Any]] | None,
) -> tuple[str, ...]:
    if not claim_spans:
        return ()
    lines = text.splitlines()
    segments: list[str] = []
    for claim_id in sorted(claim_spans):
        span = claim_spans[claim_id]
        start_value = span.get("start", span.get("line_start"))
        end_value = span.get("end", span.get("line_end", start_value))
        if not isinstance(start_value, int) or not isinstance(end_value, int):
            continue
        start = max(1, start_value)
        end = min(len(lines), max(start, end_value))
        if start <= len(lines):
            segment = "\n".join(lines[start - 1 : end]).strip()
            if segment:
                segments.append(segment)
    return tuple(segments)


@dataclass(frozen=True, slots=True)
class InvariantSnapshot:
    protected_blocks: tuple[str, ...]
    citations: tuple[str, ...]
    numbers: tuple[str, ...]
    claim_texts: tuple[str, ...]
    claim_segments: tuple[str, ...]
    semantic_sha256: str

    @classmethod
    def capture(
        cls,
        tex_text: str,
        *,
        scientific_memory: Any | None = None,
        claim_spans: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> InvariantSnapshot:
        try:
            protected = extract_protected_blocks(tex_text)
        except ProtectedBlockViolation as exc:
            raise PublicationInvariantViolation(
                f"invalid protected block markers: {exc}"
            ) from exc
        semantic = _semantic_text(tex_text)
        marked_claims = tuple(
            match.group(1).strip()
            for match in _CLAIM_MACRO_PATTERN.finditer(tex_text)
            if match.group(1).strip()
        ) + tuple(
            match.group(1).strip()
            for match in _CLAIM_BLOCK_PATTERN.finditer(tex_text)
            if match.group(1).strip()
        )
        memory_claims = tuple(
            claim
            for claim in _claim_texts_from_memory(scientific_memory, claim_spans)
            if re.sub(r"\s+", " ", claim).strip().casefold() in semantic.casefold()
        )
        return cls(
            protected_blocks=protected,
            citations=_citations(tex_text),
            numbers=_content_numbers(tex_text),
            claim_texts=memory_claims + marked_claims,
            claim_segments=_claim_span_segments(tex_text, claim_spans),
            semantic_sha256=hashlib.sha256(semantic.encode("utf-8")).hexdigest(),
        )

    def verify(self, candidate_text: str) -> None:
        try:
            protected = extract_protected_blocks(candidate_text)
        except ProtectedBlockViolation as exc:
            raise PublicationInvariantViolation(
                f"protected block markers became invalid: {exc}"
            ) from exc
        if protected != self.protected_blocks:
            raise PublicationInvariantViolation("protected block content changed")

        citations = _citations(candidate_text)
        if citations != self.citations:
            raise PublicationInvariantViolation(
                "citation keys or citation order changed during layout repair"
            )

        numbers = _content_numbers(candidate_text)
        if numbers != self.numbers:
            raise PublicationInvariantViolation(
                "scientific numbers changed during layout repair"
            )

        semantic = _semantic_text(candidate_text)
        normalized_semantic = semantic.casefold()
        for claim in self.claim_texts:
            normalized_claim = re.sub(r"\s+", " ", claim).strip().casefold()
            if normalized_claim and normalized_claim not in normalized_semantic:
                raise PublicationInvariantViolation(
                    f"scientific claim changed or disappeared: {claim[:80]}"
                )
        for segment in self.claim_segments:
            normalized_segment = _semantic_text(segment).casefold()
            if normalized_segment and normalized_segment not in normalized_semantic:
                raise PublicationInvariantViolation(
                    "claim-mapped TeX span changed during layout repair"
                )

        semantic_sha = hashlib.sha256(semantic.encode("utf-8")).hexdigest()
        if semantic_sha != self.semantic_sha256:
            raise PublicationInvariantViolation(
                "claim/prose content changed during constrained layout repair"
            )
