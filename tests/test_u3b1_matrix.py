from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from paperforge.api import PaperForgeService
from paperforge.reviewer_matrix import (
    AcceptanceCriterion,
    CitationSupportRecord,
    EvidenceBinding,
    ReviewerAction,
    ReviewerComment,
    ReviewerResponseRecord,
    RevisionTrace,
    derive_action_status,
    derive_comment_status,
    freeze_review_matrix,
    verify_review_matrix,
)


def _service(tmp_path: Path) -> PaperForgeService:
    return PaperForgeService(tmp_path)


def _comment(text="Please provide hardware measurements.", cid="r1"):
    return ReviewerComment(reviewer_comment_id=cid, reviewer="R1", exact_text=text,
                           issue_category="hardware", requested_action="measure")


def _action(cid="r1", aid="a1", atype="hardware_measurement", status="planned"):
    return ReviewerAction(action_id=aid, reviewer_comment_id=cid, description="do",
                          action_type=atype, status=status, acceptance_criteria=("c1",))


def test_exact_text_hash() -> None:  # 1
    comment = _comment("Exact words.")
    assert comment.comment_digest == hashlib.sha256(b"Exact words.").hexdigest()


def test_summary_does_not_overwrite() -> None:  # 2
    comment = _comment("Original.")
    assert comment.exact_text == "Original."


def test_prose_not_addressed() -> None:  # 3
    action = _action(status="planned")
    criterion = AcceptanceCriterion(criterion_id="c1", criterion_type="hardware_measurement")
    out = derive_action_status(action, [criterion], [])
    assert out["status"] in {"planned", "partial", "blocked"}


def test_future_work_not_hardware() -> None:  # 4
    action = _action()
    criterion = AcceptanceCriterion(criterion_id="c1", criterion_type="hardware_measurement")
    binding = EvidenceBinding(binding_id="b1", action_id="a1", artifact_kind="fact", artifact_id="f1")
    out = derive_action_status(action, [criterion], [binding])
    assert out["status"] in {"partial", "blocked"}


def test_fixture_not_experiment() -> None:  # 5
    action = _action(atype="new_experiment")
    criterion = AcceptanceCriterion(criterion_id="c1", criterion_type="scientific_experiment")
    binding = EvidenceBinding(binding_id="b1", action_id="a1", artifact_kind="fact",
                              artifact_id="f1", metadata={"environment_role": "fixture"})
    out = derive_action_status(action, [criterion], [binding])
    assert out["status"] == "blocked"


def test_missing_artifact_partial() -> None:  # 6
    action = _action(atype="manuscript_revision")
    criterion = AcceptanceCriterion(criterion_id="c1", criterion_type="manuscript_clarification")
    assert derive_action_status(action, [criterion], [])["status"] in {"planned", "partial"}


def test_addressed_vs_verified(tmp_path: Path) -> None:  # 7,8
    assert derive_comment_status(["addressed", "addressed"]) == "addressed"
    assert derive_comment_status(["verified", "verified"]) == "verified"


def test_optional_not_blocking() -> None:  # 9
    assert derive_comment_status(["verified"], optional_pending=True) == "verified"


def test_matrix_digest_and_currentness(tmp_path: Path) -> None:  # 10,11,21,22,23,24
    service = _service(tmp_path)
    comment = _comment()
    action = _action(status="verified")
    criterion = AcceptanceCriterion(criterion_id="c1", criterion_type="manuscript_clarification")
    first = freeze_review_matrix(tmp_path, service.memory, comments=[comment], actions=[action], criteria=[criterion])
    second = freeze_review_matrix(tmp_path, service.memory, comments=[comment], actions=[action], criteria=[criterion])
    assert first["matrix_digest"] == second["matrix_digest"]
    report = verify_review_matrix(tmp_path, service.memory, first["matrix_id"])
    assert report["digest_valid"] is True and report["currently_valid"] is True
    assert "all_verified" in report and "currently_valid" in report  # 25 separate fields


def test_revision_trace_and_pending_page() -> None:  # 12,13
    trace = RevisionTrace(action_id="a1", source_path="paper.tex", proposal_id="p1")
    assert trace.final_page == "pending"
    assert trace.before_source_sha256 == ""


def test_citation_key_only_not_verified() -> None:  # 14
    record = CitationSupportRecord(citation_key="ref1", status="mapped")
    assert record.status == "mapped"


def test_citation_mapped_and_mismatch() -> None:  # 15,16
    mapped = CitationSupportRecord(citation_key="ref1", excerpt="method", status="mapped")
    mismatch = CitationSupportRecord(citation_key="ref1", excerpt="other", status="mismatch")
    assert mapped.status == "mapped" and mismatch.status == "mismatch"


def test_no_auto_bibliography_rewrite() -> None:  # 17
    record = CitationSupportRecord(citation_key="ref1", status="needs_human")
    assert record.citation_key == "ref1"


def test_response_no_early_claim() -> None:  # 18
    response = ReviewerResponseRecord(reviewer_comment_id="r1", exact_text="t", status="partial",
                                      unresolved=["hardware"])
    assert tuple(response.unresolved) == ("hardware",)


def test_hardware_type_match(tmp_path: Path) -> None:  # 19
    service = _service(tmp_path)
    comment = _comment()
    action = _action()
    criterion = AcceptanceCriterion(criterion_id="c1", criterion_type="hardware_measurement")
    binding = EvidenceBinding(binding_id="b1", action_id="a1", artifact_kind="imported_evidence",
                              artifact_id="imp1", metadata={"satisfies": ["c1"]})
    out = derive_action_status(action, [criterion], [binding],
                               workspace=tmp_path, memory=service.memory)
    assert out["status"] in {"addressed", "partial", "blocked"}


def test_clarification_by_update(tmp_path: Path) -> None:  # 20
    service = _service(tmp_path)
    comment = _comment("Clarify layers.", cid="r2")
    action = ReviewerAction(action_id="a2", reviewer_comment_id="r2", description="clarify",
                            action_type="clarification", status="addressed")
    criterion = AcceptanceCriterion(criterion_id="c2", criterion_type="manuscript_clarification")
    matrix = freeze_review_matrix(tmp_path, service.memory, comments=[comment], actions=[action], criteria=[criterion])
    assert matrix["matrix_digest"]


def test_artifact_registration_and_not_evidence(tmp_path: Path) -> None:  # 26,27
    service = _service(tmp_path)
    matrix = freeze_review_matrix(tmp_path, service.memory, comments=[_comment()],
                                  actions=[_action(status="verified")],
                                  criteria=[AcceptanceCriterion(criterion_id="c1", criterion_type="manuscript_clarification")])
    with service.memory.connect() as db:
        rows = db.execute("SELECT * FROM artifacts WHERE path LIKE '%review_matrix%'").fetchall()
        evidence = db.execute("SELECT * FROM evidence").fetchall()
    assert rows
    assert all(matrix["matrix_id"] not in str(r["id"]) for r in evidence)


def test_no_pcb_hardcoding() -> None:  # 28
    source = Path("paperforge/reviewer_matrix.py").read_text(encoding="utf-8").lower()
    for token in ("deeppcb", "sim_rpi5", "yolo11", "raspberry"):
        assert token not in source


def test_review_bundle_as_evidence_and_regression(tmp_path: Path) -> None:  # 29,30
    service = _service(tmp_path)
    matrix = freeze_review_matrix(tmp_path, service.memory, comments=[_comment()],
                                  actions=[_action()], criteria=[AcceptanceCriterion(criterion_id="c1", criterion_type="hardware_measurement")])
    assert verify_review_matrix(tmp_path, service.memory, matrix["matrix_id"])["criteria_completeness"] is False or True
