from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from engine.secret_redaction import contains_secret

from .artifacts import ArtifactRecord, ArtifactStore
from .path_safety import atomic_write_text, reject_symlink_components
from .protected_blocks import (
    PROTECTED_END,
    PROTECTED_START,
    ProtectedBlockViolation,
    extract_protected_blocks,
    protected_blocks_sha256,
)

_FENCE = re.compile(r"\A```(?:latex|tex)?\s*\n(?P<body>.*)\n```\s*\Z", re.DOTALL)
_CITATION = re.compile(
    r"\\cite[a-zA-Z*]*\s*(?:\[[^\]]*\]\s*){0,2}\{([^{}]+)\}"
)
_BIB_ENTRY = re.compile(r"@\w+\s*\{\s*([^,\s]+)\s*,", re.IGNORECASE)
_FORBIDDEN_LATEX = re.compile(
    r"\\(?:input|include|write18|openout|immediate|ShellEscape)\b"
    r"|\\usepackage(?:\[[^\]]*\])?\{shellesc\}",
    re.IGNORECASE,
)
_EXPERIMENT_SECTION = re.compile(
    r"\\(?:section|subsection|subsubsection)\*?\s*\{[^{}]*"
    r"(?:experiment|evaluation|result|ablation|benchmark)[^{}]*\}",
    re.IGNORECASE,
)
_QUANTITATIVE_RESULT = re.compile(
    r"(?:\b(?:accuracy|precision|recall|f1|psnr|ssim|bleu|rouge|auc)\b"
    r"[^.\n]{0,80}\d|\d+(?:\.\d+)?\s*\\?%|\b(?:outperform|state-of-the-art)\b)",
    re.IGNORECASE,
)
_SAFE_RUN_ID = re.compile(r"[^a-zA-Z0-9_.-]+")
_BRIEF_FIELDS = ("title", "topic", "abstract", "instructions")


class WritingError(RuntimeError):
    pass


class WritingSafetyError(WritingError):
    pass


@dataclass(frozen=True)
class WritingResult:
    artifact: ArtifactRecord
    source_path: str | None
    protected_sha256: str
    request_sha256: str
    response_sha256: str

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["artifact"] = self.artifact.to_dict()
        return payload


def _without_protected_blocks(text: str) -> str:
    pattern = re.compile(
        rf"{re.escape(PROTECTED_START)}.*?{re.escape(PROTECTED_END)}",
        re.DOTALL,
    )
    return pattern.sub("", text)


def _strip_fence(text: str) -> str:
    normalized = text.strip()
    match = _FENCE.fullmatch(normalized)
    return match.group("body").strip() if match is not None else normalized


def _latex_skeleton(title: str) -> str:
    safe_title = title.strip() or "Untitled Research Manuscript"
    return (
        "\\documentclass{article}\n"
        "\\usepackage[T1]{fontenc}\n"
        "\\usepackage{graphicx}\n"
        "\\title{" + safe_title + "}\n"
        "\\author{}\n"
        "\\date{}\n"
        "\\begin{document}\n"
        "\\maketitle\n"
        "\\begin{abstract}\n"
        "Draft abstract pending evidence-backed writing.\n"
        "\\end{abstract}\n"
        "\\section{Introduction}\n"
        "Draft introduction pending evidence-backed writing.\n"
        "\\section{Method}\n"
        "Draft method description pending evidence-backed writing.\n"
        f"{PROTECTED_START}\n"
        "% Author-supplied experimental setup and results remain unchanged.\n"
        f"{PROTECTED_END}\n"
        "\\section{Conclusion}\n"
        "Draft conclusion pending evidence-backed writing.\n"
        "\\bibliographystyle{plain}\n"
        "\\bibliography{references}\n"
        "\\end{document}\n"
    )


class WritingEngine:
    """One-request, evidence-gated LaTeX writer for the writing-only runtime."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        memory: Any,
        request_text: Callable[..., str],
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve(strict=True)
        self.memory = memory
        self.request_text = request_text

    def _workspace_document(self, raw_path: str | Path) -> tuple[Path, str]:
        supplied = Path(raw_path).expanduser()
        lexical = supplied if supplied.is_absolute() else self.workspace / supplied
        lexical = reject_symlink_components(lexical, anchor=self.workspace)
        try:
            relative = lexical.relative_to(self.workspace).as_posix()
        except ValueError as exc:
            raise WritingSafetyError("writing document leaves the workspace") from exc
        if lexical.suffix.lower() != ".tex":
            raise WritingSafetyError("writing target must be a TeX document")
        if lexical.is_symlink() or (lexical.exists() and not lexical.is_file()):
            raise WritingSafetyError("writing target must be a regular file")
        return lexical, relative

    def _source_document(
        self,
        payload: Mapping[str, Any],
        *,
        run_id: str,
    ) -> tuple[Path | None, str, str]:
        raw_source = (
            payload.get("main_tex")
            or payload.get("main_tex_path")
            or payload.get("document_path")
        )
        if raw_source is not None:
            source, relative = self._workspace_document(str(raw_source))
            if not source.is_file():
                raise FileNotFoundError(source)
            return source, relative, source.read_text(encoding="utf-8")
        safe_run_id = _SAFE_RUN_ID.sub("-", run_id).strip(".-_") or "draft"
        relative = f".paperforge/drafts/{safe_run_id}.tex"
        target, relative = self._workspace_document(relative)
        return None, relative, _latex_skeleton(str(payload.get("title") or ""))

    @staticmethod
    def _brief(payload: Mapping[str, Any]) -> str:
        lines = []
        for field in _BRIEF_FIELDS:
            value = str(payload.get(field) or "").strip()
            if value:
                lines.append(f"{field}: {value}")
        outline = payload.get("outline")
        if isinstance(outline, Sequence) and not isinstance(outline, str | bytes):
            normalized = [str(item).strip() for item in outline if str(item).strip()]
            if normalized:
                lines.append("outline:\n- " + "\n- ".join(normalized))
        return "\n".join(lines)

    def _bibliography_keys(self, payload: Mapping[str, Any]) -> frozenset[str]:
        raw_path = payload.get("bibliography_path") or "references.bib"
        supplied = Path(str(raw_path)).expanduser()
        lexical = supplied if supplied.is_absolute() else self.workspace / supplied
        lexical = reject_symlink_components(lexical, anchor=self.workspace)
        if lexical.is_symlink() or (lexical.exists() and not lexical.is_file()):
            raise WritingSafetyError("bibliography must be a regular file")
        if not lexical.exists():
            return frozenset()
        return frozenset(_BIB_ENTRY.findall(lexical.read_text(encoding="utf-8")))

    @staticmethod
    def _validate_candidate(
        candidate: str,
        *,
        protected_blocks: tuple[str, ...],
        bibliography_keys: frozenset[str],
    ) -> None:
        if len(candidate.encode("utf-8")) > 2 * 1024 * 1024:
            raise WritingSafetyError("generated TeX exceeds the 2 MiB safety limit")
        if contains_secret(candidate):
            raise WritingSafetyError("generated TeX contains secret-like content")
        for required in (
            r"\documentclass",
            r"\begin{document}",
            r"\end{document}",
        ):
            if required not in candidate:
                raise WritingSafetyError(f"generated TeX is missing {required}")
        if _FORBIDDEN_LATEX.search(candidate):
            raise WritingSafetyError("generated TeX contains an unsafe file or shell command")
        try:
            candidate_blocks = extract_protected_blocks(candidate)
        except ProtectedBlockViolation as exc:
            raise WritingSafetyError(str(exc)) from exc
        if candidate_blocks != protected_blocks:
            raise WritingSafetyError("generated TeX changed the protected experiment block")
        prose = _without_protected_blocks(candidate)
        if _EXPERIMENT_SECTION.search(prose) or _QUANTITATIVE_RESULT.search(prose):
            raise WritingSafetyError(
                "writing-only output introduced an unprotected experimental claim"
            )
        cited = {
            key.strip()
            for group in _CITATION.findall(candidate)
            for key in group.split(",")
            if key.strip()
        }
        unknown = sorted(cited - bibliography_keys)
        if unknown:
            raise WritingSafetyError(
                "generated TeX cites unknown bibliography keys: " + ", ".join(unknown)
            )

    def write(
        self,
        *,
        run_id: str,
        payload: Mapping[str, Any],
        model: str,
    ) -> WritingResult:
        source, relative_path, original = self._source_document(
            payload,
            run_id=run_id,
        )
        brief = self._brief(payload)
        if source is None and not brief:
            raise WritingError("writing request requires a topic, title, abstract, or instructions")
        try:
            protected_blocks = extract_protected_blocks(original)
        except ProtectedBlockViolation as exc:
            raise WritingSafetyError(str(exc)) from exc
        if not protected_blocks:
            raise WritingSafetyError(
                "writing-only editing requires a protected experiment block"
            )
        bibliography_keys = self._bibliography_keys(payload)
        prompt = (
            "Return one complete LaTeX document and nothing else.\n"
            "Edit only non-experimental prose. Preserve every PAPERFORGE protected "
            "experiment block byte-for-byte. Do not add experiments, results, "
            "metrics, comparisons, citations not present in the allowed key list, "
            "AI disclosure text, shell commands, file includes, or new bibliography "
            "entries.\n\n"
            f"Writing brief:\n{brief or 'Refine the existing non-experimental prose.'}\n\n"
            f"Allowed citation keys: {', '.join(sorted(bibliography_keys)) or '<none>'}\n\n"
            f"Current document:\n{original}"
        )
        if len(prompt.encode("utf-8")) > 3 * 1024 * 1024:
            raise WritingSafetyError("writing request exceeds the 3 MiB context limit")
        response = self.request_text(
            model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You edit evidence-gated scientific LaTeX. Follow the "
                        "structural restrictions exactly."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            stage="writeup",
            max_tokens=4096,
            temperature=0,
        )
        candidate = _strip_fence(response)
        self._validate_candidate(
            candidate,
            protected_blocks=protected_blocks,
            bibliography_keys=bibliography_keys,
        )
        before_sha256 = protected_blocks_sha256(original)
        atomic_write_text(self.workspace, relative_path, candidate + "\n")
        written = self.workspace / relative_path
        try:
            after_sha256 = protected_blocks_sha256(
                written.read_text(encoding="utf-8")
            )
            if after_sha256 != before_sha256:
                raise WritingSafetyError(
                    "protected experiment hash changed after the write"
                )
            store = ArtifactStore(
                self.workspace,
                allowed_roots=(relative_path,),
                allowed_suffixes=(".tex",),
                allowed_kinds=("latex",),
                memory=self.memory,
            )
            artifact = store.register(
                relative_path,
                kind="latex",
                status="DRAFT_REQUIRES_CLAIM_GATE",
                media_type="application/x-tex",
                metadata={
                    "protected_sha256": after_sha256,
                    "workflow_id": run_id,
                    "source_path": (
                        source.relative_to(self.workspace).as_posix()
                        if source is not None
                        else None
                    ),
                },
            )
        except BaseException:
            if source is not None:
                atomic_write_text(self.workspace, relative_path, original)
            else:
                written.unlink(missing_ok=True)
            raise
        return WritingResult(
            artifact=artifact,
            source_path=(
                source.relative_to(self.workspace).as_posix()
                if source is not None
                else None
            ),
            protected_sha256=after_sha256,
            request_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            response_sha256=hashlib.sha256(candidate.encode("utf-8")).hexdigest(),
        )


__all__ = [
    "WritingEngine",
    "WritingError",
    "WritingResult",
    "WritingSafetyError",
]
