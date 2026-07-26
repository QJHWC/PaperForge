from __future__ import annotations

from pathlib import Path

import pytest

from paperforge.api import PaperForgeService
from paperforge.writing import WritingEngine, WritingSafetyError


def _document(prose: str, protected: str = "Author-owned results.") -> str:
    return (
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        f"{prose}\n"
        "% PAPERFORGE-PROTECTED-EXPERIMENT-START\n"
        f"{protected}\n"
        "% PAPERFORGE-PROTECTED-EXPERIMENT-END\n"
        "\\bibliographystyle{plain}\n"
        "\\bibliography{references}\n"
        "\\end{document}\n"
    )


def test_writing_rolls_back_when_artifact_registration_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tex = tmp_path / "paper.tex"
    original = _document("Original prose.")
    tex.write_text(original, encoding="utf-8")
    candidate = _document("Changed prose.")
    service = PaperForgeService(tmp_path)
    engine = WritingEngine(
        tmp_path,
        memory=service.memory,
        request_text=lambda *_args, **_kwargs: candidate,
    )

    def fail_registration(*_args: object, **_kwargs: object) -> object:
        raise OSError("artifact database unavailable")

    monkeypatch.setattr(
        "paperforge.writing.ArtifactStore.register",
        fail_registration,
    )
    with pytest.raises(OSError, match="artifact database unavailable"):
        engine.write(
            run_id="workflow-fixture",
            payload={
                "main_tex": "paper.tex",
                "instructions": "Improve the prose.",
            },
            model="bailu-turing",
        )

    assert tex.read_text(encoding="utf-8") == original


def test_writing_rejects_protected_experiment_changes_before_write(
    tmp_path: Path,
) -> None:
    tex = tmp_path / "paper.tex"
    original = _document("Original prose.")
    tex.write_text(original, encoding="utf-8")
    candidate = _document("Changed prose.", protected="Invented result.")
    service = PaperForgeService(tmp_path)
    engine = WritingEngine(
        tmp_path,
        memory=service.memory,
        request_text=lambda *_args, **_kwargs: candidate,
    )

    with pytest.raises(WritingSafetyError, match="protected experiment"):
        engine.write(
            run_id="workflow-fixture",
            payload={
                "main_tex": "paper.tex",
                "instructions": "Improve the prose.",
            },
            model="bailu-turing",
        )

    assert tex.read_text(encoding="utf-8") == original
