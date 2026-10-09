import hashlib
import re
from pathlib import Path

import pytest
from test_u1_patch import _document
from test_u24a_finalization import _spec_with_table

from paperforge.api import PaperForgeService
from paperforge.grounded_narrative import (
    NarrativeGroundingError,
    NarrativeSentence,
    render_narrative,
)
from paperforge.protected_blocks import extract_protected_blocks
from paperforge.result_update import (
    ResultUpdateError,
    apply_proposal,
    approve_proposal,
    build_proposal,
)
from paperforge.writing import WritingEngine, WritingSafetyError
from paperforge.writing_patch import PatchSafetyError, apply_patch

REJECTED = (
    r"\InputIfFileExists{fixture.tex}{}{}",
    r"\IfFileExists{fixture.tex}{}{}",
    r"\VerbatimInput{fixture.txt}",
    r"\lstinputlisting{fixture.txt}",
    r"\includegraphics{fixture.pdf}",
    r"\@input{fixture.tex}",
    r"\csname input\endcsname{fixture.tex}",
    r"^^5cinput{fixture.tex}",
    r"\catcode`\!=0 !input{fixture.tex}",
    r"\def\loader{\input} Text.",
    r"\let\loader\input Text.",
    r"\openin1=fixture.tex",
    r"\write1{fixture}",
    r"\directlua{print('fixture')}",
    r"\scantokens{fixture}",
    r"\begin{filecontents}{fixture.txt}fixture\end{filecontents}",
    r"\usepackage {shellesc}",
)


@pytest.mark.parametrize("text", REJECTED)
def test_all_generated_tex_entrypoints_reject_dynamic_or_file_commands(tmp_path: Path, text: str) -> None:
    original = _document("Original prose.")
    tex = tmp_path / "paper.tex"
    tex.write_text(original)
    service = PaperForgeService(tmp_path)
    with pytest.raises(PatchSafetyError, match="unsafe"):
        apply_patch(tmp_path, memory=service.memory, request_text=lambda *a, **k: text,
                    run_id="fixture-run", target="paper.tex",
                    source_sha256=hashlib.sha256(tex.read_bytes()).hexdigest(), anchor="Original prose.")
    assert tex.read_text() == original
    with pytest.raises(WritingSafetyError, match="unsafe"):
        WritingEngine._validate_candidate(_document(text),
                                          protected_blocks=extract_protected_blocks(original),
                                          bibliography_keys=frozenset())
    with pytest.raises(NarrativeGroundingError, match="forbidden LaTeX"):
        render_narrative([[NarrativeSentence("fixture", text, role="transition")]])
    workspace = tmp_path / "proposal"
    workspace.mkdir()
    service, spec, old, _ = _spec_with_table(workspace, "Original prose.")
    with pytest.raises(ResultUpdateError, match="unsafe"):
        build_proposal(workspace, service.memory, spec, proposed_block=old.replace("Original prose.", text))
    assert not (workspace / ".paperforge" / "result_updates").exists()


def test_safe_math_and_formatting_patch_still_work(tmp_path: Path) -> None:
    original = _document("Original prose.")
    tex = tmp_path / "paper.tex"
    tex.write_text(original)
    service = PaperForgeService(tmp_path)
    replacement = r"A \textbf{bounded} statement with $x^{2}+\alpha_1+\theta_{i}$ and \emph{no claim}."
    apply_patch(tmp_path, memory=service.memory, request_text=lambda *a, **k: replacement,
                run_id="fixture-run", target="paper.tex",
                source_sha256=hashlib.sha256(tex.read_bytes()).hexdigest(), anchor="Original prose.")
    assert replacement in tex.read_text()


def test_historical_proposal_is_rechecked_before_apply(tmp_path: Path, monkeypatch) -> None:
    from paperforge import latex_safety

    service, spec, old, _ = _spec_with_table(tmp_path, "Original prose.")
    original = (tmp_path / "paper.tex").read_bytes()
    # Simulate a historical producer lacking the new policy, only in a test DB.
    with monkeypatch.context() as legacy:
        legacy.setattr(latex_safety, "FORBIDDEN_LATEX", re.compile(r"(?!)"))
        proposal = build_proposal(tmp_path, service.memory, spec,
                                  proposed_block=old.replace("Original prose.", r"\InputIfFileExists{fixture.tex}{}{}"))
    approve_proposal(tmp_path, service.memory, proposal["proposal_id"], approver="fixture-owner")
    with pytest.raises(ResultUpdateError, match="unsafe"):
        apply_proposal(tmp_path, service.memory, proposal["proposal_id"])
    assert (tmp_path / "paper.tex").read_bytes() == original
    with service.memory.connect() as db:
        row = db.execute("SELECT state FROM result_updates WHERE id=?", (proposal["proposal_id"],)).fetchone()
    assert row["state"] == "APPROVED"
