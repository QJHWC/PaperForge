from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Literal

from paperforge.claim_manifest import import_latex_claims
from paperforge.models import ClaimStatus, ClaimType
from paperforge.publication import PublicationEngine
from paperforge.publication.visual_checks import (
    inspect_page_structure,
    inspect_rendered_pages,
)
from paperforge.release import ReleaseVerifier, write_page_inspection
from paperforge.scientific_memory import ScientificMemory

CVPR_COMMIT = "291758547e923160eb4d37079b7b9f0dfce82355"
CVPR_TREE = "bada7af3a66da84fd610948fd72ce5dd01fb3cc2"
CVPR_ARCHIVE_SHA256 = (
    "72df21fe120ab08c59980bc9461c6cafc427149e6400684749e731086efce5d6"
)


def _git_output(repository: Path, *args: str, text: bool = True) -> str | bytes:
    completed = subprocess.run(
        ("git", "-C", str(repository), *args),
        check=True,
        capture_output=True,
        text=text,
    )
    return completed.stdout


def verify_cvpr_author_kit(repository: Path) -> None:
    root = repository.expanduser().resolve()
    if str(_git_output(root, "rev-parse", "HEAD")).strip() != CVPR_COMMIT:
        raise ValueError("CVPR author-kit commit does not match the lock")
    if (
        str(_git_output(root, "rev-parse", "HEAD^{tree}")).strip()
        != CVPR_TREE
    ):
        raise ValueError("CVPR author-kit tree does not match the lock")
    archive = _git_output(root, "archive", "--format=tar", "HEAD", text=False)
    if not isinstance(archive, bytes):
        raise TypeError("git archive did not return bytes")
    if hashlib.sha256(archive).hexdigest() != CVPR_ARCHIVE_SHA256:
        raise ValueError("CVPR author-kit archive does not match the lock")
    if not (root / "cvpr.sty").is_file():
        raise FileNotFoundError(root / "cvpr.sty")
    license_files = [
        path
        for pattern in ("LICENSE*", "COPYING*", "NOTICE*")
        for path in root.glob(pattern)
        if path.is_file()
    ]
    if license_files:
        raise ValueError(
            "CVPR lock policy expected no license file at the pinned commit"
        )


BibliographyState = Literal["empty", "existing"]
BIBLIOGRAPHY_STATES: tuple[BibliographyState, ...] = ("empty", "existing")


def _main_tex(profile: str, bibliography_state: BibliographyState) -> str:
    if profile == "generic":
        preamble = r"\documentclass{article}"
    elif profile == "cvpr":
        preamble = (
            "\\documentclass[10pt,twocolumn,letterpaper]{article}\n"
            "\\usepackage[review]{cvpr}\n"
            "\\def\\paperID{1}\n"
            "\\def\\confName{CVPR}\n"
            "\\def\\confYear{2026}"
        )
    elif profile == "ieee":
        preamble = r"\documentclass[conference]{IEEEtran}"
    elif profile == "elsevier":
        preamble = r"\documentclass{elsarticle}"
    else:
        raise ValueError(f"unsupported publication profile: {profile}")
    body = (
        f"The production publication engine validates the {profile} profile "
        "from locked source inputs."
    )
    bibliography = ""
    if bibliography_state == "existing":
        body += r" Its evidence path is cited~\cite{fixture}."
        bibliography = (
            "\\bibliographystyle{plain}\n"
            "\\bibliography{references}\n"
        )
    return (
        f"{preamble}\n"
        f"\\title{{PaperForge {profile} validation}}\n"
        "\\author{PaperForge Validation}\n"
        "\\begin{document}\n"
        "\\maketitle\n"
        f"{body}\n"
        f"{bibliography}"
        "\\end{document}\n"
    )


def _references_bib(state: BibliographyState) -> str:
    if state == "empty":
        return ""
    return (
        "@article{fixture,\n"
        "  author = {PaperForge Validation},\n"
        "  title = {Locked Publication Validation Fixture},\n"
        "  journal = {PaperForge Validation Journal},\n"
        "  year = {2026}\n"
        "}\n"
    )


def _validate_profile(
    root: Path,
    profile: str,
    bibliography_state: BibliographyState,
    *,
    cvpr_author_kit: Path,
) -> dict[str, Any]:
    workspace = root / profile / bibliography_state
    project = workspace / "paper"
    project.mkdir(parents=True)
    (project / "main.tex").write_text(
        _main_tex(profile, bibliography_state),
        encoding="utf-8",
    )
    (project / "references.bib").write_text(
        _references_bib(bibliography_state),
        encoding="utf-8",
    )
    if profile == "cvpr":
        shutil.copy2(cvpr_author_kit / "cvpr.sty", project / "cvpr.sty")

    memory = ScientificMemory(workspace / ".paperforge" / "paperforge.db")
    evidence = memory.add_evidence(
        evidence_type="SOURCE_CODE",
        excerpt=f"locked {profile} publication validation fixture",
    )
    claim_manifest = import_latex_claims(
        memory,
        project / "main.tex",
        evidence_resolver=lambda unit: (
            ClaimType.STATIC_IMPLEMENTATION,
            ClaimStatus.SUPPORTED_STATIC,
            (evidence,),
        ),
    )
    result = PublicationEngine().publish(
        project,
        template=profile,
        scientific_memory=memory,
        artifact_dir=workspace / "dist",
        release_root=workspace,
    )
    if (
        not result.success
        or result.final_pdf is None
        or not result.rounds
        or result.rounds[-1].render_result is None
    ):
        return {
            "passed": False,
            "profile": profile,
            "bibliography_state": bibliography_state,
            "diagnostics": [issue.code for issue in result.diagnostics],
        }
    render = result.rounds[-1].render_result
    render_integrity = inspect_rendered_pages(render.pages)
    structural_review = inspect_page_structure(
        result.final_pdf,
        render.pages,
        render_integrity=render_integrity,
        expected_text_by_page={
            1: (f"PaperForge {profile} validation",),
        },
    )
    if not render_integrity["passed"] or not structural_review["passed"]:
        return {
            "passed": False,
            "profile": profile,
            "bibliography_state": bibliography_state,
            "diagnostics": ["automated_structural_inspection_failed"],
            "render_integrity": render_integrity,
            "structural_review": structural_review,
        }
    write_page_inspection(
        workspace,
        pdf_path=result.final_pdf,
        rendered_pages=render.pages,
        reviewer="PaperForge deterministic structural page reviewer",
        inspection_kind="automated-structural",
        render_integrity=render_integrity,
        structural_review=structural_review,
    )
    release_gate = ReleaseVerifier(workspace, memory=memory).verify()
    return {
        "passed": release_gate.passed,
        "profile": profile,
        "bibliography_state": bibliography_state,
        "claim_count": len(claim_manifest["claims"]),
        "page_count": render.page_count,
        "render_integrity": render_integrity,
        "structural_review": structural_review,
        "release_gate": release_gate.to_dict(),
    }


def validate_profiles(cvpr_author_kit: Path) -> dict[str, Any]:
    verify_cvpr_author_kit(cvpr_author_kit)
    with tempfile.TemporaryDirectory(
        prefix="paperforge-publication-profiles-"
    ) as temporary:
        root = Path(temporary)
        profiles = {
            profile: {
                state: _validate_profile(
                    root,
                    profile,
                    state,
                    cvpr_author_kit=cvpr_author_kit,
                )
                for state in BIBLIOGRAPHY_STATES
            }
            for profile in ("generic", "cvpr", "ieee", "elsevier")
        }
    return {
        "schema": "paperforge.publication-profile-validation/v1",
        "passed": all(
            result["passed"]
            for states in profiles.values()
            for result in states.values()
        ),
        "cvpr_lock": {
            "commit": CVPR_COMMIT,
            "tree": CVPR_TREE,
            "git_archive_sha256": CVPR_ARCHIVE_SHA256,
            "distributed": False,
        },
        "profiles": profiles,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cvpr-author-kit", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = validate_profiles(args.cvpr_author_kit)
    rendered = json.dumps(
        result,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
