from __future__ import annotations

from pathlib import Path

import pytest

from paperforge.protected_blocks import (
    PROTECTED_END,
    PROTECTED_START,
    ProtectedBlockViolation,
    ProtectedCoder,
    ProtectedEditTransaction,
    protected_blocks_sha256,
)


def _document(body: str = "No experimental results are available.") -> str:
    return (
        "\\section{Method}\nOriginal method.\n"
        f"{PROTECTED_START}\n"
        "\\section{Results}\n"
        f"{body}\n"
        f"{PROTECTED_END}\n"
        "\\section{Conclusion}\nOriginal conclusion.\n"
    )


def test_protected_transaction_allows_edits_outside_block(tmp_path: Path) -> None:
    tex = tmp_path / "main.tex"
    tex.write_text(_document(), encoding="utf-8")
    before_hash = protected_blocks_sha256(tex.read_text(encoding="utf-8"))
    transaction = ProtectedEditTransaction(tex, require_markers=True)

    tex.write_text(
        tex.read_text(encoding="utf-8").replace("Original conclusion.", "Revised conclusion."),
        encoding="utf-8",
    )

    assert transaction.verify() == before_hash
    assert "Revised conclusion." in tex.read_text(encoding="utf-8")


def test_protected_transaction_restores_tampered_document(tmp_path: Path) -> None:
    tex = tmp_path / "main.tex"
    original = _document()
    tex.write_text(original, encoding="utf-8")
    transaction = ProtectedEditTransaction(tex, require_markers=True)
    tex.write_text(_document("Fabricated score: 99.9."), encoding="utf-8")

    with pytest.raises(ProtectedBlockViolation):
        transaction.verify()

    assert tex.read_text(encoding="utf-8") == original


def test_protected_coder_checks_every_model_edit(tmp_path: Path) -> None:
    tex = tmp_path / "main.tex"
    original = _document()
    tex.write_text(original, encoding="utf-8")

    class TamperingCoder:
        def run(self, prompt: str) -> str:
            tex.write_text(_document("Fabricated metric."), encoding="utf-8")
            return "done"

    coder = ProtectedCoder(TamperingCoder(), tex, require_markers=True)
    with pytest.raises(ProtectedBlockViolation):
        coder.run("rewrite")

    assert tex.read_text(encoding="utf-8") == original


def test_protected_coder_rolls_back_when_model_raises(tmp_path: Path) -> None:
    tex = tmp_path / "main.tex"
    original = _document()
    tex.write_text(original, encoding="utf-8")

    class FailingCoder:
        def run(self, prompt: str) -> str:
            tex.write_text(_document().replace("Original method.", "Partial edit."), encoding="utf-8")
            raise RuntimeError("model failed")

    coder = ProtectedCoder(FailingCoder(), tex, require_markers=True)
    with pytest.raises(RuntimeError):
        coder.run("rewrite")

    assert tex.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    "text",
    (
        f"{PROTECTED_END}\n{PROTECTED_START}",
        f"{PROTECTED_START}\n{PROTECTED_START}\n{PROTECTED_END}\n{PROTECTED_END}",
        f"{PROTECTED_START}\nmissing end",
    ),
)
def test_marker_parser_rejects_malformed_order(text: str) -> None:
    with pytest.raises(ProtectedBlockViolation):
        protected_blocks_sha256(text)
