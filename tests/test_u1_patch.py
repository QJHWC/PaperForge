from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from paperforge.api import PaperForgeService
from paperforge.writing_patch import (
    PatchSafetyError,
    UnsupportedMultiFileEdit,
    apply_patch,
)


def _document(prose: str, protected: str = "Author-owned results.") -> str:
    return (
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        f"{prose}\n"
        "% PAPERFORGE-PROTECTED-EXPERIMENT-START\n"
        f"{protected}\n"
        "% PAPERFORGE-PROTECTED-EXPERIMENT-END\n"
        "\\end{document}\n"
    )


def test_patch_rejects_stale_hash(tmp_path: Path) -> None:
    tex = tmp_path / "paper.tex"
    tex.write_text(_document("Original anchor sentence here."), encoding="utf-8")
    service = PaperForgeService(tmp_path)
    with pytest.raises(PatchSafetyError, match="source changed"):
        apply_patch(
            tmp_path,
            memory=service.memory,
            request_text=lambda *a, **k: "replacement",
            run_id="wf-1",
            target="paper.tex",
            source_sha256="0" * 64,
            anchor="Original anchor sentence here.",
        )
    assert "Original anchor" in tex.read_text(encoding="utf-8")


def test_patch_rejects_ambiguous_anchor(tmp_path: Path) -> None:
    tex = tmp_path / "paper.tex"
    tex.write_text(_document("Repeat. Repeat."), encoding="utf-8")
    sha = hashlib.sha256(tex.read_bytes()).hexdigest()
    service = PaperForgeService(tmp_path)
    with pytest.raises(PatchSafetyError, match="not unique"):
        apply_patch(
            tmp_path,
            memory=service.memory,
            request_text=lambda *a, **k: "replacement",
            run_id="wf-1",
            target="paper.tex",
            source_sha256=sha,
            anchor="Repeat.",
        )


def test_patch_rejects_truncated_output(tmp_path: Path) -> None:
    tex = tmp_path / "paper.tex"
    original = _document("Original anchor sentence here.")
    tex.write_text(original, encoding="utf-8")
    sha = hashlib.sha256(tex.read_bytes()).hexdigest()
    service = PaperForgeService(tmp_path)
    with pytest.raises(PatchSafetyError, match="truncated"):
        apply_patch(
            tmp_path,
            memory=service.memory,
            request_text=lambda *a, **k: "Unfinished \\begin{itemize}",
            run_id="wf-1",
            target="paper.tex",
            source_sha256=sha,
            anchor="Original anchor sentence here.",
        )
    assert tex.read_text(encoding="utf-8") == original


def test_patch_rejects_multi_target(tmp_path: Path) -> None:
    tex = tmp_path / "paper.tex"
    tex.write_text(_document("Hello."), encoding="utf-8")
    sha = hashlib.sha256(tex.read_bytes()).hexdigest()
    service = PaperForgeService(tmp_path)
    with pytest.raises(UnsupportedMultiFileEdit, match="UNSUPPORTED_MULTI_FILE_EDIT"):
        apply_patch(
            tmp_path,
            memory=service.memory,
            request_text=lambda *a, **k: "x",
            run_id="wf-1",
            target="paper.tex",
            source_sha256=sha,
            anchor="Hello.",
            extra_targets=["other.tex"],
        )


def test_patch_applies_single_file(tmp_path: Path) -> None:
    tex = tmp_path / "paper.tex"
    tex.write_text(_document("Original anchor sentence here."), encoding="utf-8")
    sha = hashlib.sha256(tex.read_bytes()).hexdigest()
    service = PaperForgeService(tmp_path)
    result = apply_patch(
        tmp_path,
        memory=service.memory,
        request_text=lambda *a, **k: "Revised anchor sentence.",
        run_id="wf-1",
        target="paper.tex",
        source_sha256=sha,
        anchor="Original anchor sentence here.",
        instructions="Tighten prose.",
    )
    assert "Revised anchor" in tex.read_text(encoding="utf-8")
    assert result.target_path == "paper.tex"
