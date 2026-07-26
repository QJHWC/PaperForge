from pathlib import Path

import pytest

from paperforge.claim_manifest import (
    ClaimManifestError,
    extract_latex_claim_units,
    import_latex_claims,
)
from paperforge.models import ClaimStatus, ClaimType
from paperforge.scientific_memory import ScientificMemory


def test_latex_claim_import_assigns_every_sentence_and_item(tmp_path: Path) -> None:
    tex = tmp_path / "main.tex"
    tex.write_text(
        r"""
\documentclass{article}
\begin{document}
\begin{abstract}
First supported sentence. Second supported sentence.
\end{abstract}
\section{Method}
\begin{itemize}
\item One supported item;
\item Another supported item.
\end{itemize}
\bibliographystyle{plain}
\bibliography{references}
\end{document}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    units = extract_latex_claim_units(tex)
    assert len(units) == 4

    memory = ScientificMemory(tmp_path / "memory.db")
    source = memory.add_source(kind="SOURCE", uri="source.py")
    evidence = memory.add_evidence(
        evidence_type="SOURCE_CODE",
        source_id=source,
        excerpt="verified implementation",
    )
    manifest = import_latex_claims(
        memory,
        tex,
        evidence_resolver=lambda unit: (
            ClaimType.STATIC_IMPLEMENTATION,
            ClaimStatus.SUPPORTED_STATIC,
            (evidence,),
        ),
    )
    assert manifest["coverage"]["percent"] == 100.0
    assert memory.claim_gate()["passed"]


def test_latex_claim_import_follows_nested_inputs_and_records_non_claims(
    tmp_path: Path,
) -> None:
    sections = tmp_path / "sections"
    sections.mkdir()
    (sections / "method.tex").write_text(
        "The implementation uses two stages.\n"
        "\\input{details}\n",
        encoding="utf-8",
    )
    (sections / "details.tex").write_text(
        "Acknowledgments.\n",
        encoding="utf-8",
    )
    tex = tmp_path / "main.tex"
    tex.write_text(
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        "\\maketitle\n"
        "\\input{sections/method}\n"
        "\\end{document}\n",
        encoding="utf-8",
    )

    units = extract_latex_claim_units(tex)
    assert [(unit.file, unit.text) for unit in units] == [
        ("sections/method.tex", "The implementation uses two stages."),
        ("sections/details.tex", "Acknowledgments."),
    ]

    memory = ScientificMemory(tmp_path / "memory.db")
    evidence = memory.add_evidence(
        evidence_type="SOURCE_CODE",
        excerpt="two stages",
    )

    def classify(unit):
        if unit.text == "Acknowledgments.":
            return (
                ClaimType.NON_CLAIM,
                ClaimStatus.NON_CLAIM,
                (),
                "acknowledgment",
            )
        return (
            ClaimType.STATIC_IMPLEMENTATION,
            ClaimStatus.SUPPORTED_STATIC,
            (evidence,),
        )

    review_id = memory.approve_non_claim(
        text="Acknowledgments.",
        category="acknowledgment",
        reviewer="test-reviewer",
    )
    manifest = import_latex_claims(
        memory,
        tex,
        evidence_resolver=classify,
        non_claim_review_id=review_id,
    )
    assert manifest["coverage"]["mapped_claims"] == 2
    assert manifest["coverage"]["percent"] == 100.0
    gate = memory.claim_gate()
    assert gate["passed"]
    assert gate["public_claim_count"] == 1
    assert gate["non_claim_count"] == 1


def test_non_claim_cannot_hide_a_performance_assertion(tmp_path: Path) -> None:
    tex = tmp_path / "main.tex"
    tex.write_text(
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        "The model improves accuracy by 10 percent.\n"
        "\\end{document}\n",
        encoding="utf-8",
    )
    memory = ScientificMemory(tmp_path / "memory.db")

    with pytest.raises(ValueError, match="persisted approval review"):
        import_latex_claims(
            memory,
            tex,
            evidence_resolver=lambda unit: (
                ClaimType.NON_CLAIM,
                ClaimStatus.NON_CLAIM,
                (),
                "acknowledgment",
            ),
        )

    with pytest.raises(ValueError, match="controlled structure"):
        memory.approve_non_claim(
            text="We thank the model because it improves accuracy by 99 percent.",
            category="acknowledgment",
            reviewer="test-reviewer",
        )


def test_claim_extraction_keeps_appendix_after_bibliography_and_chinese(
    tmp_path: Path,
) -> None:
    tex = tmp_path / "main.tex"
    tex.write_text(
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        "Main statement.\n"
        "\\bibliography{references}\n"
        "\\appendix\n"
        "附录结论有效。第二个结论也有效！\n"
        "\\end{document}\n",
        encoding="utf-8",
    )

    assert [unit.text for unit in extract_latex_claim_units(tex)] == [
        "Main statement.",
        "附录结论有效。",
        "第二个结论也有效！",
    ]


def test_claim_extraction_keeps_prose_after_inline_structure_commands(
    tmp_path: Path,
) -> None:
    tex = tmp_path / "main.tex"
    tex.write_text(
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        "\\section{Results} The model improves accuracy by 99 percent.\n"
        "\\paragraph{Limits}\\label{sec:limits} The result is unverified.\n"
        "\\bibliography{references} The appendix statement remains public.\n"
        "\\end{document}\n",
        encoding="utf-8",
    )

    assert [unit.text for unit in extract_latex_claim_units(tex)] == [
        "The model improves accuracy by 99 percent.",
        "The result is unverified.",
        "The appendix statement remains public.",
    ]


def test_latex_claim_import_rejects_cyclic_or_inline_inputs(
    tmp_path: Path,
) -> None:
    tex = tmp_path / "main.tex"
    included = tmp_path / "included.tex"
    tex.write_text(
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        "\\input{included}\n"
        "\\end{document}\n",
        encoding="utf-8",
    )
    included.write_text("\\input{main}\n", encoding="utf-8")
    with pytest.raises(ClaimManifestError, match="cyclic"):
        extract_latex_claim_units(tex)

    included.write_text(
        "Text before \\input{other} text after.\n",
        encoding="utf-8",
    )
    with pytest.raises(ClaimManifestError, match="alone on a line"):
        extract_latex_claim_units(tex)


def test_latex_claim_import_does_not_treat_includegraphics_as_input(
    tmp_path: Path,
) -> None:
    tex = tmp_path / "main.tex"
    tex.write_text(
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        "\\includegraphics{figure.pdf}\n"
        "The figure is generated from verified source data.\n"
        "\\end{document}\n",
        encoding="utf-8",
    )

    units = extract_latex_claim_units(tex)

    assert [unit.text for unit in units] == [
        "The figure is generated from verified source data."
    ]
