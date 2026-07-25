from __future__ import annotations

import json
from pathlib import Path

import pytest

from paperforge.api import PaperForgeService
from paperforge.artifacts import sha256_file
from paperforge.cli import main
from paperforge.models import (
    ClaimStatus,
    ClaimType,
    CompletionGate,
    ExecutionProfile,
    WorkflowStatus,
)
from paperforge.publication import SourceBundler
from paperforge.publication.engine import PUBLICATION_MANIFEST_SCHEMA
from paperforge.release import write_page_inspection
from paperforge.workflow import InvalidTransition


def test_cli_rejects_secret_flags_without_echoing_value(capsys) -> None:
    secret = "canary-not-for-output"
    with pytest.raises(SystemExit) as raised:
        main(["preflight", f"--openai-api-key={secret}"])
    assert secret not in str(raised.value)
    assert secret not in capsys.readouterr().out


def test_service_persists_and_resumes_writing_only(tmp_path: Path) -> None:
    service = PaperForgeService(tmp_path)
    handle = service.run(profile=ExecutionProfile.WRITING_ONLY)
    assert handle.status == WorkflowStatus.RUNNING.value
    assert handle.checkpoint == "runtime"
    assert handle.metadata["runtime_executed"] is True
    assert (tmp_path / handle.metadata["runtime_report"]).is_file()
    assert "paper" in handle.metadata["completed_roles"]
    assert service.status(handle.run_id).run_id == handle.run_id

    service.workflow.transition(handle.run_id, WorkflowStatus.INTERRUPTED)
    resumed = service.resume(handle.run_id)
    assert resumed.status == WorkflowStatus.RUNNING.value


def test_full_profile_waits_for_approval(tmp_path: Path) -> None:
    service = PaperForgeService(tmp_path)
    handle = service.run(profile=ExecutionProfile.FULL)
    assert handle.status == WorkflowStatus.AWAITING_APPROVAL.value
    proposal_id = handle.metadata["proposal_id"]
    with pytest.raises(PermissionError):
        service.resume(handle.run_id)
    service.approve(proposal_id)
    assert service.resume(handle.run_id).status == WorkflowStatus.RUNNING.value


def test_preflight_emits_machine_readable_status(tmp_path: Path, capsys) -> None:
    assert main(["preflight", "--workspace", str(tmp_path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "CODE_VERIFIED"


def test_release_recomputes_authoritative_gate_and_rejects_caller_gate(
    tmp_path: Path,
) -> None:
    service = PaperForgeService(tmp_path)
    handle = service.run(profile=ExecutionProfile.WRITING_ONLY)
    (tmp_path / "main.tex").write_text(
        "\\documentclass{article}\n"
        "\\begin{document}\nVerified statement.\n"
        "\\bibliographystyle{plain}\n"
        "\\bibliography{references}\n"
        "\\end{document}\n",
        encoding="utf-8",
    )
    (tmp_path / "references.bib").write_text("", encoding="utf-8")
    source = service.memory.add_source(kind="TEST", uri="fixture://release")
    evidence = service.memory.add_evidence(
        evidence_type="SOURCE_CODE",
        source_id=source,
        excerpt="verified statement",
    )
    claim = service.memory.add_claim(
        claim_type=ClaimType.STATIC_IMPLEMENTATION,
        text="Verified statement.",
        status=ClaimStatus.SUPPORTED_STATIC,
        metadata={"tex_span": {"file": "main.tex", "start": 3, "end": 3}},
    )
    service.memory.link_claim(claim, evidence)

    bundle = SourceBundler().build(
        tmp_path,
        tmp_path / "dist" / "paper-source.zip",
    )
    pdf = tmp_path / "main.pdf"
    pdf.write_bytes(b"%PDF-1.4 fixture")
    page = tmp_path / ".paperforge" / "publication" / "page-1.png"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_bytes(b"png")
    claim_gate = service.memory.claim_gate()
    claim_gate["coverage"] = {"passed": True, "percent": 100.0}
    gates = {
        "claim_gate": claim_gate,
        "compile": True,
        "render": True,
        "diagnostics": True,
        "invariants": True,
        "source_lock": True,
    }
    manifest = {
        "schema": PUBLICATION_MANIFEST_SCHEMA,
        "status": "passed",
        "project_root": ".",
        "gates": gates,
        "rounds": [
            {
                "render": {
                    "success": True,
                    "page_count": 1,
                    "rendered_pages": [str(page)],
                }
            }
        ],
        "artifacts": {
            "pdf": {"path": "main.pdf", "sha256": sha256_file(pdf)},
            "source_bundle": {
                "path": "dist/paper-source.zip",
                "sha256": bundle.sha256,
                "source_lock_path": str(
                    bundle.source_lock_path.relative_to(tmp_path)
                ),
            },
        },
    }
    (tmp_path / "dist" / "publication.manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    write_page_inspection(
        tmp_path,
        pdf_path=pdf,
        rendered_pages=(page,),
        reviewer="test reviewer",
    )

    with pytest.raises(InvalidTransition, match="caller-supplied"):
        service.release(run_id=handle.run_id, gate=CompletionGate())

    released = service.release(run_id=handle.run_id)
    assert released.status == WorkflowStatus.COMPLETED.value
    report = json.loads(
        (tmp_path / ".paperforge" / "release_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert report["gate"]["release_manifest_verified"]
