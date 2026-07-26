from __future__ import annotations

import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from paperforge.claim_manifest import import_latex_claims
from paperforge.models import ClaimStatus, ClaimType
from paperforge.protected_blocks import PROTECTED_END, PROTECTED_START
from paperforge.publication import (
    DEFAULT_TEMPLATE_REGISTRY,
    BibliographyContractError,
    CompileResult,
    InvariantSnapshot,
    LayoutDiagnosis,
    PublicationCompiler,
    PublicationEngine,
    PublicationGateError,
    PublicationInvariantViolation,
    PublicationIssue,
    RenderResult,
    SourceBundler,
    Toolchain,
    discover_toolchain,
    publish,
    verify_source_lock,
)
from paperforge.scientific_memory import ScientificMemory


def _project(tmp_path: Path, body: str = "Accuracy is 91.2\\% \\cite{smith2024}.") -> Path:
    project = tmp_path / "paper"
    project.mkdir()
    (project / "main.tex").write_text(
        "\\documentclass{article}\n"
        "\\setlength{\\textfloatsep}{12pt}\n"
        "\\begin{document}\n"
        f"{body}\n"
        "\\bibliographystyle{plain}\n"
        "\\bibliography{references}\n"
        "\\end{document}\n",
        encoding="utf-8",
    )
    (project / "references.bib").write_text(
        "@article{smith2024,\n"
        "  title={Evidence},\n"
        "  author={Smith, A.},\n"
        "  year={2024}\n"
        "}\n",
        encoding="utf-8",
    )
    return project


def _toolchain() -> Toolchain:
    return Toolchain(
        tex=Path("/tools/tex"),
        latexmk=Path("/tools/latexmk"),
        pdflatex=Path("/tools/pdflatex"),
        bibtex=Path("/tools/bibtex"),
        pdftoppm=Path("/tools/pdftoppm"),
        pdfinfo=Path("/tools/pdfinfo"),
        pdftotext=Path("/tools/pdftotext"),
    )


def _publication_memory(project: Path, tmp_path: Path) -> tuple[ScientificMemory, dict]:
    memory = ScientificMemory(tmp_path / f"{project.name}-memory.db")
    source_id = memory.add_source(kind="TEST_FIXTURE", uri="fixture://publication")
    evidence_id = memory.add_evidence(
        evidence_type="SOURCE_CODE",
        source_id=source_id,
        excerpt="Deterministic publication test evidence.",
    )
    manifest = import_latex_claims(
        memory,
        project / "main.tex",
        evidence_resolver=lambda unit: (
            ClaimType.STATIC_IMPLEMENTATION,
            ClaimStatus.SUPPORTED_STATIC,
            (evidence_id,),
        ),
    )
    spans = {
        claim["claim_id"]: claim["tex_span"]
        for claim in manifest["claims"]
    }
    return memory, spans


def test_template_profile_registry_contains_supported_profiles() -> None:
    assert DEFAULT_TEMPLATE_REGISTRY.names() == ("cvpr", "elsevier", "generic", "ieee")
    assert DEFAULT_TEMPLATE_REGISTRY.detect("\\documentclass[conference]{IEEEtran}").name == "ieee"
    assert DEFAULT_TEMPLATE_REGISTRY.detect("\\documentclass{cvpr}").name == "cvpr"
    assert DEFAULT_TEMPLATE_REGISTRY.detect("\\documentclass{elsarticle}").name == "elsevier"
    assert DEFAULT_TEMPLATE_REGISTRY.detect("\\documentclass{article}").name == "generic"
    assert DEFAULT_TEMPLATE_REGISTRY.get("IEEEtran").name == "ieee"


def test_single_references_bib_contract_rejects_other_bibliographies(tmp_path: Path) -> None:
    project = _project(tmp_path)
    (project / "extra.bib").write_text("@misc{extra, title={No}}", encoding="utf-8")

    compiler = PublicationCompiler(_toolchain())

    with pytest.raises(BibliographyContractError, match="references.bib"):
        compiler.validate_project(project, "main.tex")


def test_toolchain_discovery_honors_explicit_cross_platform_paths(tmp_path: Path) -> None:
    bin_dir = tmp_path / "portable-tools"
    bin_dir.mkdir()
    names = ("tex", "latexmk", "pdflatex", "bibtex", "pdftoppm", "pdfinfo", "pdftotext")
    for name in names:
        executable = bin_dir / f"{name}.exe"
        executable.write_text("", encoding="utf-8")

    toolchain = discover_toolchain(
        env={
            "PAPERFORGE_TEX_BIN": str(bin_dir),
            "PAPERFORGE_POPPLER_BIN": str(bin_dir),
            "PATH": "",
        },
        platform_name="Windows",
    )

    assert toolchain.latexmk == bin_dir / "latexmk.exe"
    assert toolchain.pdflatex == bin_dir / "pdflatex.exe"
    assert toolchain.bibtex == bin_dir / "bibtex.exe"
    assert toolchain.pdftoppm == bin_dir / "pdftoppm.exe"


def test_compile_deletes_stale_pdf_and_requires_zero_return_code(tmp_path: Path) -> None:
    project = _project(tmp_path)
    stale_pdf = project / "main.pdf"
    stale_pdf.write_bytes(b"stale")
    saw_deleted: list[bool] = []

    def runner(command, *, cwd, **kwargs):
        saw_deleted.append(not stale_pdf.exists())
        return subprocess.CompletedProcess(command, 1, stdout="fatal", stderr="failed")

    result = PublicationCompiler(_toolchain(), runner=runner).compile(project)

    assert saw_deleted == [True]
    assert not result.success
    assert not stale_pdf.exists()
    assert any(issue.code == "COMMAND_FAILED" for issue in result.diagnostics)
    assert "returncode: 1" in result.log_path.read_text(encoding="utf-8")


def test_pdflatex_fallback_checks_bibtex_and_every_compile_pass(tmp_path: Path) -> None:
    project = _project(tmp_path)
    toolchain = _toolchain()
    toolchain = Toolchain(
        tex=toolchain.tex,
        latexmk=None,
        pdflatex=toolchain.pdflatex,
        bibtex=toolchain.bibtex,
        pdftoppm=toolchain.pdftoppm,
        pdfinfo=toolchain.pdfinfo,
        pdftotext=toolchain.pdftotext,
    )
    commands: list[str] = []

    def runner(command, *, cwd, **kwargs):
        commands.append(Path(command[0]).name)
        if len(commands) == 4:
            Path(cwd, "main.pdf").write_bytes(b"%PDF-new")
            Path(cwd, "main.log").write_text("clean", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    result = PublicationCompiler(toolchain, runner=runner).compile(project)

    assert result.success
    assert commands == ["pdflatex", "bibtex", "pdflatex", "pdflatex"]
    assert [command.returncode for command in result.commands] == [0, 0, 0, 0]


def test_compile_moves_named_output_without_leaving_duplicate_pdf(tmp_path: Path) -> None:
    project = _project(tmp_path)
    destination = tmp_path / "release" / "paper.pdf"

    def runner(command, *, cwd, **kwargs):
        Path(cwd, "main.pdf").write_bytes(b"%PDF-new")
        Path(cwd, "main.log").write_text("clean", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    result = PublicationCompiler(_toolchain(), runner=runner).compile(
        project,
        output_pdf=destination,
    )

    assert result.success
    assert result.pdf_path == destination.resolve()
    assert destination.read_bytes() == b"%PDF-new"
    assert not (project / "main.pdf").exists()


@pytest.mark.parametrize(
    ("latex_log", "expected_code"),
    (
        (
            "LaTeX Warning: Citation `missing' on page 1 undefined.\n"
            "LaTeX Warning: There were undefined references.\n",
            "UNRESOLVED_REFERENCE",
        ),
        ("Overfull \\hbox (14.0pt too wide) in paragraph at lines 3--4\n", "OVERFLOW"),
    ),
)
def test_compile_rejects_unresolved_references_and_overflow(
    tmp_path: Path,
    latex_log: str,
    expected_code: str,
) -> None:
    project = _project(tmp_path)

    def runner(command, *, cwd, **kwargs):
        Path(cwd, "main.pdf").write_bytes(b"%PDF-new")
        Path(cwd, "main.log").write_text(latex_log, encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    result = PublicationCompiler(_toolchain(), runner=runner).compile(project)

    assert not result.success
    assert any(issue.code == expected_code for issue in result.diagnostics)
    assert result.pdf_path is None
    assert not (project / "main.pdf").exists()


def test_invariant_snapshot_allows_layout_only_changes() -> None:
    original = (
        "\\setlength{\\textfloatsep}{12pt}\n"
        f"{PROTECTED_START}\nMeasured accuracy is 91.2\\% \\cite{{smith2024}}.\n{PROTECTED_END}\n"
    )
    changed = original.replace("{12pt}", "{10pt}")

    snapshot = InvariantSnapshot.capture(original)

    snapshot.verify(changed)


@pytest.mark.parametrize(
    "changed",
    (
        "Measured accuracy is 92.1\\% \\cite{smith2024}.",
        "Measured accuracy is 91.2\\% \\cite{other2024}.",
        "Measured performance is 91.2\\% \\cite{smith2024}.",
    ),
)
def test_invariant_snapshot_rejects_number_citation_and_claim_changes(changed: str) -> None:
    original = "Measured accuracy is 91.2\\% \\cite{smith2024}."
    snapshot = InvariantSnapshot.capture(original)

    with pytest.raises(PublicationInvariantViolation):
        snapshot.verify(changed)


def test_invariant_snapshot_rejects_protected_block_changes() -> None:
    original = f"{PROTECTED_START}\nMeasured 91.2.\n{PROTECTED_END}\n"
    snapshot = InvariantSnapshot.capture(original)

    with pytest.raises(PublicationInvariantViolation, match="protected"):
        snapshot.verify(original.replace("91.2", "92.1"))


def test_publication_engine_runs_at_most_three_verified_rounds(tmp_path: Path) -> None:
    project = _project(tmp_path)
    memory, spans = _publication_memory(project, tmp_path)

    class FakeCompiler:
        toolchain = _toolchain()

        def __init__(self) -> None:
            self.calls = 0

        def validate_project(self, project_dir, main_tex):
            return None

        def compile(self, project_dir, main_tex="main.tex", output_pdf=None):
            self.calls += 1
            pdf = Path(project_dir) / "main.pdf"
            log = Path(project_dir) / "publication-compile.log"
            pdf.write_bytes(b"%PDF")
            log.write_text("ok", encoding="utf-8")
            return CompileResult(
                success=True,
                pdf_path=pdf,
                log_path=log,
                diagnostics=(),
            )

    class FakeRenderer:
        def render(self, pdf_path, output_dir):
            output = Path(output_dir)
            output.mkdir(parents=True, exist_ok=True)
            page = output / "page-1.png"
            page.write_bytes(b"png")
            log = output / "render.log"
            log.write_text("ok", encoding="utf-8")
            return RenderResult(
                success=True,
                pages=(page,),
                page_count=1,
                log_path=log,
            )

    class Diagnostician:
        def __init__(self) -> None:
            self.calls = 0

        def diagnose(self, compile_result, render_result, profile):
            self.calls += 1
            issues = (
                ()
                if self.calls == 3
                else (PublicationIssue("PAGE_FIT", "layout still needs tightening"),)
            )
            return LayoutDiagnosis(issues=issues)

    repairs: list[int] = []

    def repairer(context):
        repairs.append(context.round_number)
        value = 12 - context.round_number
        return context.source_text.replace(
            f"{{{value + 1}pt}}",
            f"{{{value}pt}}",
        )

    compiler = FakeCompiler()
    result = PublicationEngine(
        compiler=compiler,
        renderer=FakeRenderer(),
        diagnostician=Diagnostician(),
        repairer=repairer,
        max_rounds=9,
    ).publish(
        project,
        profile="generic",
        scientific_memory=memory,
        claim_spans=spans,
    )

    assert result.success
    assert compiler.calls == 3
    assert repairs == [1, 2]
    assert len(result.rounds) == 3
    assert result.manifest_path is not None and result.manifest_path.exists()
    assert result.bundle_path is not None and result.bundle_path.exists()
    assert result.source_lock_path is not None and result.source_lock_path.exists()
    assert result.gates["claim_gate"]["passed"]
    assert result.gates["compile"]
    assert result.gates["render"]
    assert result.gates["source_lock"]
    assert result.artifacts["pdf"] == result.final_pdf
    publication_manifest = json.loads(
        result.manifest_path.read_text(encoding="utf-8")
    )
    assert publication_manifest["project_root"] == "."
    assert publication_manifest["source_invariants"]["entrypoint_sha256"]
    assert publication_manifest["source_invariants"]["bibliography_sha256"]
    assert publication_manifest["source_invariants"]["semantic_sha256"]
    assert str(tmp_path) not in json.dumps(publication_manifest["rounds"])
    assert callable(publish)


def test_publication_engine_enforces_scientific_memory_claim_gate(tmp_path: Path) -> None:
    project = _project(tmp_path)
    memory = ScientificMemory(tmp_path / "paperforge.db")
    memory.add_claim(
        claim_type=ClaimType.EXPERIMENT_RESULT,
        text="Unsupported improvement claim.",
        status=ClaimStatus.BLOCKED,
    )

    with pytest.raises(PublicationGateError, match="claim gate"):
        PublicationEngine(max_rounds=1).publish(
            project,
            scientific_memory=memory,
            claim_gate={"passed": True, "claim_count": 1, "failures": []},
        )

    assert not (project / "main.pdf").exists()


def test_publication_engine_rolls_back_repairer_file_side_effects(tmp_path: Path) -> None:
    project = _project(tmp_path)
    memory, spans = _publication_memory(project, tmp_path)
    references = project / "references.bib"
    original_references = references.read_bytes()

    def compile_runner(command, *, cwd, **kwargs):
        Path(cwd, "main.pdf").write_bytes(b"%PDF-new")
        Path(cwd, "main.log").write_text("clean", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    class FakeRenderer:
        def render(self, pdf_path, output_dir):
            output = Path(output_dir)
            output.mkdir(parents=True, exist_ok=True)
            page = output / "page-1.png"
            page.write_bytes(b"png")
            log = output / "render.log"
            log.write_text("ok", encoding="utf-8")
            return RenderResult(True, (page,), 1, log)

    def diagnose(compile_result, render_result, profile):
        return LayoutDiagnosis((PublicationIssue("PAGE_FIT", "tighten layout"),))

    def malicious_repair(context):
        references.write_text("tampered", encoding="utf-8")
        return context.source_text.replace("{12pt}", "{11pt}")

    result = PublicationEngine(
        compiler=PublicationCompiler(_toolchain(), runner=compile_runner),
        renderer=FakeRenderer(),
        diagnostician=diagnose,
        repairer=malicious_repair,
    ).publish(project, scientific_memory=memory, claim_spans=spans)

    assert not result.success
    assert result.diagnostics[-1].code == "REPAIR_SIDE_EFFECT"
    assert references.read_bytes() == original_references


def test_publication_engine_fails_closed_without_scientific_memory(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)

    with pytest.raises(PublicationGateError, match="scientific claim gate"):
        PublicationEngine(max_rounds=1).publish(project)


def test_source_bundle_is_deterministic_allowlisted_and_self_verifying(tmp_path: Path) -> None:
    project = _project(tmp_path)
    (project / "figure.png").write_bytes(b"image")
    (project / ".env").write_text("SECRET=do-not-package", encoding="utf-8")
    (project / ".secret.tex").write_text("do-not-package", encoding="utf-8")
    (project / "main.pdf").write_bytes(b"generated")
    (project / "camera-ready.pdf").write_bytes(b"generated-output")
    (project / "figure.pdf").write_bytes(b"source-figure")
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"
    bundler = SourceBundler()

    bundle_one = bundler.build(
        project,
        first,
        profile="generic",
        excluded_paths=("camera-ready.pdf",),
    )
    bundler.build(
        project,
        second,
        profile="generic",
        excluded_paths=("camera-ready.pdf",),
    )

    assert first.read_bytes() == second.read_bytes()
    assert bundle_one.sha256 == hashlib.sha256(first.read_bytes()).hexdigest()
    with zipfile.ZipFile(first) as archive:
        names = archive.namelist()
        assert names == sorted(names)
        assert "main.tex" in names
        assert "references.bib" in names
        assert "figure.png" in names
        assert "figure.pdf" in names
        assert ".env" not in names
        assert ".secret.tex" not in names
        assert "main.pdf" not in names
        assert "camera-ready.pdf" not in names
        assert "publication.source.lock.json" in names
        assert "SHA256SUMS" in names
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist())

    verification = verify_source_lock(project, bundle_one.source_lock_path)
    assert verification.valid
    lock = json.loads(bundle_one.source_lock_path.read_text(encoding="utf-8"))
    assert lock["dependency_policy"] == {
        "floating_references_allowed": False,
        "mode": "vendored",
        "network_required": False,
    }


def test_source_lock_detects_tampering(tmp_path: Path) -> None:
    project = _project(tmp_path)
    bundle = SourceBundler().build(project, tmp_path / "paper.zip")
    (project / "main.tex").write_text("tampered", encoding="utf-8")

    verification = verify_source_lock(project, bundle.source_lock_path)

    assert not verification.valid
    assert verification.mismatches[0].path == "main.tex"
