from pathlib import Path

from paperforge.claim_manifest import extract_latex_claim_units, import_latex_claims
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
