from __future__ import annotations

from pathlib import Path

from engine.template_migration import (
    import_source_draft,
    import_template_directory,
    load_workspace_history,
    recycle_workspace_artifact,
    restore_recycled_artifact,
    run_template_migration,
)


def _source_tex() -> bytes:
    return rb"""
\documentclass[journal,twoside,web]{ieeecolor}
\usepackage{generic}
\title{Sample Title}
\author{Alice}
\begin{document}
\maketitle
\begin{abstract}
Sample abstract.
\end{abstract}
\begin{IEEEkeywords}
alpha, beta
\end{IEEEkeywords}
\section{Intro}
Body text.
\begin{thebibliography}{1}
\bibitem{ref1} Example reference.
\end{thebibliography}
\end{document}
""".strip()


def test_import_source_draft_stores_tex_and_optional_pdf(tmp_path) -> None:
    manifest = import_source_draft(
        tmp_path,
        [
            {"filename": "source.tex", "content": _source_tex()},
            {"filename": "preview.pdf", "content": b"%PDF-1.4 preview"},
            {"filename": "figures/a.png", "content": b"png"},
        ],
    )

    draft_dir = tmp_path / "source_drafts" / manifest["draft_id"]
    assert (draft_dir / "source.tex").exists()
    assert (draft_dir / "preview.pdf").exists()
    assert (draft_dir / "assets" / "figures" / "a.png").exists()
    history = load_workspace_history(tmp_path)
    assert history["source_drafts"][0]["draft_id"] == manifest["draft_id"]


def test_import_template_dir_detects_cjc_profile(tmp_path) -> None:
    manifest = import_template_directory(
        tmp_path,
        [
            {"filename": "MyCjC/CjC.cls", "content": b"class"},
            {"filename": "MyCjC/CjC_template_tex.tex", "content": b"\\documentclass{CjC}\\begin{document}\\end{document}"},
            {"filename": "MyCjC/captionhack.sty", "content": b"% sty"},
        ],
    )
    assert manifest["profile"] == "cjc"
    assert manifest["migration_ready"] is True
    history = load_workspace_history(tmp_path)
    assert history["templates"][0]["template_id"] == manifest["template_id"]


def test_unknown_template_requires_manifest_for_migration(tmp_path) -> None:
    source = import_source_draft(tmp_path, [{"filename": "source.tex", "content": _source_tex()}])
    template = import_template_directory(
        tmp_path,
        [{"filename": "Unknown/main.tex", "content": b"\\documentclass{article}\\begin{document}\\end{document}"}],
    )
    assert template["migration_ready"] is False
    try:
        run_template_migration(tmp_path, source["draft_id"], template["template_id"], "out")
    except ValueError as exc:
        assert "not ready" in str(exc)
    else:
        raise AssertionError("expected migration to fail for unknown template without manifest")


def test_template_migration_generates_tex_and_pdf(tmp_path, monkeypatch) -> None:
    source = import_source_draft(tmp_path, [{"filename": "source.tex", "content": _source_tex()}])
    template = import_template_directory(
        tmp_path,
        [
            {"filename": "MyCjC/CjC.cls", "content": b"class"},
            {"filename": "MyCjC/CjC_template_tex.tex", "content": b"\\documentclass{CjC}\\begin{document}\\end{document}"},
            {"filename": "MyCjC/captionhack.sty", "content": b"% sty"},
        ],
    )

    def _fake_render(output_dir: Path, tex_name: str = "main.tex", pdf_name: str = "main.pdf") -> Path:
        pdf_path = output_dir / pdf_name
        pdf_path.write_bytes(b"%PDF-1.4 migrated")
        return pdf_path

    monkeypatch.setattr("engine.template_migration.render_latex_project", _fake_render)
    manifest = run_template_migration(tmp_path, source["draft_id"], template["template_id"], "demo")

    migration_dir = tmp_path / "migrations" / manifest["migration_id"]
    assert (migration_dir / "output" / "main.tex").exists()
    assert (migration_dir / "output" / "main.pdf").exists()
    assert (tmp_path / f"migrated_{template['template_id']}.tex").exists()
    assert (tmp_path / f"migrated_{template['template_id']}.pdf").exists()


def test_recycle_and_restore_migration_artifact(tmp_path, monkeypatch) -> None:
    source = import_source_draft(tmp_path, [{"filename": "source.tex", "content": _source_tex()}])
    template = import_template_directory(
        tmp_path,
        [
            {"filename": "MyCjC/CjC.cls", "content": b"class"},
            {"filename": "MyCjC/CjC_template_tex.tex", "content": b"\\documentclass{CjC}\\begin{document}\\end{document}"},
        ],
    )

    def _fake_render(output_dir: Path, tex_name: str = "main.tex", pdf_name: str = "main.pdf") -> Path:
        pdf_path = output_dir / pdf_name
        pdf_path.write_bytes(b"%PDF-1.4")
        return pdf_path

    monkeypatch.setattr("engine.template_migration.render_latex_project", _fake_render)
    manifest = run_template_migration(tmp_path, source["draft_id"], template["template_id"], "demo")

    deleted = recycle_workspace_artifact(tmp_path, "migrations", manifest["migration_id"])
    assert (tmp_path / "recycle_bin" / deleted["recycle_id"]).exists()
    restored = restore_recycled_artifact(tmp_path, deleted["recycle_id"])
    assert "restored_to" in restored
