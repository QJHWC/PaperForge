from __future__ import annotations

from paperforge.agents import AgentRequest, AgentRole
from paperforge.api import PaperForgeService
from paperforge.models import ClaimRelation, ClaimStatus, ClaimType
from paperforge.runtime import ResearchOSRuntime


def _seed_passing_gate(tmp_path) -> PaperForgeService:
    service = PaperForgeService(tmp_path)
    mem = service.memory
    cid = mem.add_claim(
        claim_type=ClaimType.STATIC_IMPLEMENTATION.value,
        text="Build script exists.",
        status=ClaimStatus.SUPPORTED_STATIC.value,
    )
    eid = mem.add_evidence(
        evidence_type="CODE",
        excerpt="script present",
        path="run.py",
    )
    mem.link_claim(cid, eid, ClaimRelation.SUPPORTS.value)
    return service


def test_reviewer_completed_is_not_scientific_verification(tmp_path) -> None:
    service = _seed_passing_gate(tmp_path)
    runtime = ResearchOSRuntime(tmp_path, profile="writing-only", memory=service.memory)
    result = runtime.registry.dispatch(
        AgentRequest(role=AgentRole.REVIEWER, task="review", payload={})
    )
    assert result.status.value == "COMPLETED"
    assert result.output["operation"] == "evidence_review"
    assert result.output["scientific_review_state"] == "needs_human_review"
    assert result.output["needs_human_review"] is True
    assert "not_scientifically_reviewed" in result.output["verification_state"]


def test_report_splits_inventory_and_review(tmp_path) -> None:
    service = _seed_passing_gate(tmp_path)
    runtime = ResearchOSRuntime(tmp_path, profile="writing-only", memory=service.memory)
    report = runtime.execute(run_id="wf-review-1", legacy_mode=None, inputs={"title": "t"})
    payload = report.to_dict()
    # Compat field stays, but new split fields prevent UI misreading.
    assert "completed_roles" in payload
    assert payload["scientific_review_state"] in {"needs_human_review", "blocked", "not_run"}
    assert "reviewer_display_hint" in payload
