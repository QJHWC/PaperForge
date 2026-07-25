from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

from engine.perform_writeup import (
    ChktexResult,
    _sanitize_template_tex_file,
    compile_latex,
    generate_latex,
    perform_writeup,
)


def test_generate_latex_accepts_inline_thebibliography_without_references_bib(tmp_path, monkeypatch) -> None:
    latex_dir = tmp_path / "latex"
    latex_dir.mkdir(parents=True)
    (latex_dir / "template.tex").write_text(
        r"""
\documentclass{article}
\begin{document}
Inline citation-free manuscript.
\begin{thebibliography}{1}
\bibitem{ref1} Example reference.
\end{thebibliography}
\end{document}
""".strip(),
        encoding="utf-8",
    )

    compile_calls: list[tuple[str, str, int, bool]] = []

    def _fake_compile(cwd: str, pdf_file: str, timeout: int = 30, use_bibtex: bool = True) -> bool:
        compile_calls.append((cwd, pdf_file, timeout, use_bibtex))
        return True

    monkeypatch.setattr("engine.perform_writeup.compile_latex", _fake_compile)
    coder = Mock()

    ok = generate_latex(coder, str(tmp_path), str(tmp_path / "paper.pdf"))

    assert ok is True
    assert compile_calls == [(str(latex_dir), str(tmp_path / "paper.pdf"), 30, False)]
    coder.run.assert_not_called()



def test_generate_latex_resolves_figures_subdirectory_paths(tmp_path, monkeypatch) -> None:
    latex_dir = tmp_path / "latex"
    figures_dir = latex_dir / "figures"
    figures_dir.mkdir(parents=True)
    (figures_dir / "example.png").write_bytes(b"png")
    (latex_dir / "template.tex").write_text(
        r"""
\documentclass{article}
\begin{document}
\begin{figure}
\includegraphics{figures/example.png}
\end{figure}
\begin{thebibliography}{1}
\bibitem{ref1} Example reference.
\end{thebibliography}
\end{document}
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "engine.perform_writeup.compile_latex",
        lambda cwd, pdf_file, timeout=30, use_bibtex=True: True,
    )
    coder = Mock()

    ok = generate_latex(coder, str(tmp_path), str(tmp_path / "paper.pdf"))

    assert ok is True
    coder.run.assert_not_called()



def test_generate_latex_resolves_figures_without_extension(tmp_path, monkeypatch) -> None:
    latex_dir = tmp_path / "latex"
    figures_dir = latex_dir / "figures"
    figures_dir.mkdir(parents=True)
    (figures_dir / "example.pdf").write_bytes(b"pdf")
    (latex_dir / "template.tex").write_text(
        r"""
\documentclass{article}
\begin{document}
\includegraphics{figures/example}
\begin{thebibliography}{1}
\bibitem{ref1} Example reference.
\end{thebibliography}
\end{document}
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "engine.perform_writeup.compile_latex",
        lambda cwd, pdf_file, timeout=30, use_bibtex=True: True,
    )
    coder = Mock()

    ok = generate_latex(coder, str(tmp_path), str(tmp_path / "paper.pdf"))

    assert ok is True
    coder.run.assert_not_called()



def test_generate_latex_uses_external_references_bib(tmp_path, monkeypatch) -> None:
    latex_dir = tmp_path / "latex"
    latex_dir.mkdir(parents=True)
    (latex_dir / "references.bib").write_text(
        """
@article{known_ref,
  title={Known Ref},
  author={Tester, A},
  year={2026}
}
""".strip(),
        encoding="utf-8",
    )
    (latex_dir / "template.tex").write_text(
        r"""
\documentclass{article}
\begin{document}
Cite test \cite{known_ref}.
\bibliographystyle{plain}
\bibliography{references}
\end{document}
""".strip(),
        encoding="utf-8",
    )

    compile_calls: list[tuple[str, str, int, bool]] = []

    def _fake_compile(cwd: str, pdf_file: str, timeout: int = 30, use_bibtex: bool = True) -> bool:
        compile_calls.append((cwd, pdf_file, timeout, use_bibtex))
        return True

    monkeypatch.setattr("engine.perform_writeup.compile_latex", _fake_compile)
    coder = Mock()

    ok = generate_latex(coder, str(tmp_path), str(tmp_path / "paper.pdf"))

    assert ok is True
    assert compile_calls == [(str(latex_dir), str(tmp_path / "paper.pdf"), 30, True)]
    coder.run.assert_not_called()


def test_existing_draft_refine_produces_pdf(tmp_path, monkeypatch) -> None:
    latex_dir = tmp_path / "latex"
    latex_dir.mkdir(parents=True)
    draft_path = tmp_path / "manuscript.tex"
    draft_text = r"""
\documentclass{article}
\begin{document}
Existing draft.
\begin{thebibliography}{1}
\bibitem{ref1} Example reference.
\end{thebibliography}
\end{document}
""".strip()
    draft_path.write_text(draft_text, encoding="utf-8")
    (latex_dir / "template.tex").write_text("placeholder", encoding="utf-8")

    def _fake_compile(cwd: str, pdf_file: str, timeout: int = 30, use_bibtex: bool = True) -> bool:
        Path(pdf_file).write_bytes(b"%PDF-1.4 test")
        return True

    monkeypatch.setattr("engine.perform_writeup.compile_latex", _fake_compile)
    monkeypatch.setenv("WRITEUP_CITE_ROUNDS", "0")
    monkeypatch.setenv("WRITEUP_SECOND_REFINEMENT", "0")

    perform_writeup(
        {"Name": "demo", "Title": "Demo", "Experiment": "Demo exp"},
        str(tmp_path),
        Mock(),
        Mock(),
        "gpt-5.4-xhigh",
        existing_draft_path=str(draft_path),
        skip_chktex_fix=True,
    )

    assert (tmp_path / "demo.pdf").exists()
    assert (latex_dir / "template.tex").read_text(encoding="utf-8") == draft_text


def test_non_experimental_writeup_skips_experiment_prompts(tmp_path, monkeypatch) -> None:
    latex_dir = tmp_path / "latex"
    latex_dir.mkdir(parents=True)
    (latex_dir / "template.tex").write_text(
        r"""
\documentclass{article}
\begin{document}
% PAPERFORGE-PROTECTED-EXPERIMENT-START
\section{Experimental Setup}
AUTHOR VERIFIED MATERIAL
\section{Results}
AUTHOR VERIFIED MATERIAL
% PAPERFORGE-PROTECTED-EXPERIMENT-END
\begin{thebibliography}{1}
\bibitem{ref1} Example reference.
\end{thebibliography}
\end{document}
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.setenv("WRITEUP_SKIP_EXPERIMENTAL_SECTIONS", "1")
    monkeypatch.setenv("WRITEUP_CITE_ROUNDS", "0")
    monkeypatch.setenv("WRITEUP_SECOND_REFINEMENT", "0")
    monkeypatch.setenv("WRITEUP_ENABLE_CHECKPOINT", "0")
    monkeypatch.setattr(
        "engine.perform_writeup.compile_latex",
        lambda cwd, pdf_file, timeout=30, use_bibtex=True: True,
    )
    coder = Mock()

    perform_writeup(
        {"Name": "demo", "Title": "Demo", "Experiment": "Demo"},
        str(tmp_path),
        coder,
        Mock(),
        "gpt-5.4-xhigh",
        skip_chktex_fix=True,
    )

    prompts = "\n".join(call.args[0] for call in coder.run.call_args_list)
    assert "Please fill in the Experimental Setup" not in prompts
    assert "Please fill in the Results" not in prompts
    assert "Do not create, rewrite, expand, or remove Experimental Setup or Results" in prompts


def test_missing_cls_autocopy(tmp_path, monkeypatch) -> None:
    latex_dir = tmp_path / "latex"
    latex_dir.mkdir(parents=True)
    (latex_dir / "template.tex").write_text(
        r"""
\documentclass{article}
\begin{document}
\begin{thebibliography}{1}
\bibitem{ref1} Example reference.
\end{thebibliography}
\end{document}
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "engine.perform_writeup.compile_latex",
        lambda cwd, pdf_file, timeout=30, use_bibtex=True: True,
    )

    ok = generate_latex(Mock(), str(tmp_path), str(tmp_path / "paper.pdf"), skip_chktex_fix=True)

    assert ok is True
    assert (latex_dir / "ieeecolor.cls").exists()
    assert (latex_dir / "generic.sty").exists()
    assert (latex_dir / "TII.eps").exists()


def test_sanitize_does_not_inject_disclosure(tmp_path) -> None:
    tex_path = tmp_path / "template.tex"
    original = r"\documentclass{article}\begin{document}Body.\end{document}"
    tex_path.write_text(original, encoding="utf-8")

    _sanitize_template_tex_file(str(tex_path))

    assert tex_path.read_text(encoding="utf-8") == original


def test_chktex_warnings_write_report_without_coder_run(tmp_path, monkeypatch) -> None:
    latex_dir = tmp_path / "latex"
    latex_dir.mkdir(parents=True)
    (latex_dir / "template.tex").write_text(
        r"""
\documentclass{article}
\begin{document}
\begin{thebibliography}{1}
\bibitem{ref1} Example reference.
\end{thebibliography}
\end{document}
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "engine.perform_writeup.compile_latex",
        lambda cwd, pdf_file, timeout=30, use_bibtex=True: True,
    )
    monkeypatch.setattr(
        "engine.perform_writeup.run_chktex",
        lambda latex_dir, tex_file: ChktexResult(errors=[], warnings=["Warning 1 at 1:1: demo"], raw_output="warning"),
    )
    coder = Mock()

    ok = generate_latex(
        coder,
        str(tmp_path),
        str(tmp_path / "paper.pdf"),
        skip_chktex_fix=True,
    )

    assert ok is True
    assert (latex_dir / "chktex_warnings.txt").read_text(encoding="utf-8").strip() == "Warning 1 at 1:1: demo"
    coder.run.assert_not_called()


def test_compile_latex_tolerates_non_utf8_output(tmp_path, monkeypatch) -> None:
    latex_dir = tmp_path / "latex"
    latex_dir.mkdir(parents=True)
    (latex_dir / "template.pdf").write_bytes(b"%PDF-1.4 test")

    calls = []

    class _Result:
        def __init__(self):
            self.stdout = "ok"
            self.stderr = ""

    def _fake_run(*args, **kwargs):
        calls.append(kwargs)
        return _Result()

    monkeypatch.setattr("engine.perform_writeup.subprocess.run", _fake_run)

    ok = compile_latex(str(latex_dir), str(tmp_path / "paper.pdf"), use_bibtex=False)

    assert ok is True
    assert calls
    assert all(call["encoding"] == "utf-8" for call in calls)
    assert all(call["errors"] == "replace" for call in calls)
    assert (tmp_path / "paper.pdf").exists()
