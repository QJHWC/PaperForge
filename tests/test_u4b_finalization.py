from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from paperforge.api import PaperForgeService
from paperforge.release_eligibility import (
    ReleasePolicy,
    ReleaseTarget,
    assessment_current,
    evaluate_release,
)


def setup_target(root):
    hashes = {}
    for name, data in {"main.tex": b"source", "refs.bib": b"bibliography", "final.pdf": b"pdf"}.items():
        (root / name).write_bytes(data)
        hashes[name] = hashlib.sha256(data).hexdigest()
    build = {
        "schema": "paperforge.build-provenance/v1",
        "status": "succeeded",
        "source_hashes": {"main.tex": hashes["main.tex"], "refs.bib": hashes["refs.bib"]},
        "output_path": "final.pdf",
        "output_sha256": hashes["final.pdf"],
        "command": ["compiler", "main.tex"],
        "toolchain": {"compiler": "test-v1"},
    }
    raw = json.dumps(build, sort_keys=True).encode()
    (root / "build.json").write_bytes(raw)
    target = ReleaseTarget(
        target_id="t1", manuscript_path="main.tex", manuscript_sha256=hashes["main.tex"],
        final_pdf_path="final.pdf", final_pdf_sha256=hashes["final.pdf"],
        bibliography_hashes={"refs.bib": hashes["refs.bib"]},
        build_manifest_path="build.json", build_digest=hashlib.sha256(raw).hexdigest(),
    )
    return PaperForgeService(root), target, ReleasePolicy(required_gates=("source_integrity",))


def test_missing_source_never_passes(tmp_path):
    service = PaperForgeService(tmp_path)
    report = evaluate_release(tmp_path, service.memory, ReleaseTarget("empty"),
                              ReleasePolicy(required_gates=("source_integrity",)))
    assert report["overall_eligibility"] == "NOT_ELIGIBLE"


def test_build_is_required_and_binds_exact_inputs(tmp_path):
    service, target, policy = setup_target(tmp_path)
    assert evaluate_release(tmp_path, service.memory, target, policy)["overall_eligibility"] == "ELIGIBLE"
    missing = replace(target, build_manifest_path="", build_digest="")
    assert evaluate_release(tmp_path, service.memory, missing, policy)["overall_eligibility"] == "NOT_ELIGIBLE"
    (tmp_path / "main.tex").write_bytes(b"new source")
    changed = replace(target, manuscript_sha256=hashlib.sha256(b"new source").hexdigest())
    assert evaluate_release(tmp_path, service.memory, changed, policy)["overall_eligibility"] == "NOT_ELIGIBLE"


def test_evaluator_has_no_filesystem_or_database_writes(tmp_path):
    service, target, policy = setup_target(tmp_path)
    with service.memory.connect() as db:
        before = list(db.iterdump())
    files = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    evaluate_release(tmp_path, service.memory, target, policy)
    with service.memory.connect() as db:
        assert list(db.iterdump()) == before
    assert {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == files


@pytest.mark.parametrize("filename", ["main.tex", "final.pdf", "refs.bib", "build.json"])
def test_assessment_rechecks_upstream_without_rewriting_history(tmp_path, filename):
    from paperforge.release_eligibility import freeze_assessment

    service, target, policy = setup_target(tmp_path)
    report = evaluate_release(tmp_path, service.memory, target, policy)
    freeze_assessment(tmp_path, service.memory, report)
    path = tmp_path / ".paperforge/release" / report["report_id"] / "report.json"
    original = path.read_bytes()
    assert assessment_current(tmp_path, service.memory, report["report_id"])["currently_valid"]
    (tmp_path / filename).write_bytes(b"changed")
    state = assessment_current(tmp_path, service.memory, report["report_id"])
    assert state["historical_integrity"] == "valid"
    assert not state["currently_valid"]
    assert path.read_bytes() == original


def test_target_timestamp_does_not_change_assessment_digest(tmp_path):
    service, target, policy = setup_target(tmp_path)
    a = evaluate_release(tmp_path, service.memory, target, policy)
    b = evaluate_release(tmp_path, service.memory, replace(target, created_at="tomorrow"), policy)
    assert a["report_digest"] == b["report_digest"]


def test_acknowledgement_is_bound_and_not_a_release(tmp_path):
    from paperforge.release_eligibility import (
        freeze_assessment,
        record_release_acknowledgement,
        release_authorization,
    )

    service, target, policy = setup_target(tmp_path)
    policy = replace(policy, require_human_owner_ack=True)
    report = evaluate_release(tmp_path, service.memory, target, policy)
    freeze_assessment(tmp_path, service.memory, report)
    assert report["overall_eligibility"] == "ELIGIBLE"
    pending = release_authorization(tmp_path, service.memory, report["report_id"])
    assert pending["authorization_state"] == "NEEDS_HUMAN"
    ack = record_release_acknowledgement(
        tmp_path, service.memory, report["report_id"], approver="owner", rationale="checked",
    )
    authorized = release_authorization(tmp_path, service.memory, report["report_id"], ack_id=ack["ack_id"])
    assert authorized["authorization_state"] == "AUTHORIZED"
    assert authorized["release_state"] == "NOT_RELEASED"
    changed_policy = replace(policy, policy_id="replacement")
    assert release_authorization(tmp_path, service.memory, report["report_id"], ack_id=ack["ack_id"],
                                 current_policy=changed_policy)["authorization_state"] == "BLOCKED"
    with pytest.raises(Exception, match="current eligible"):
        record_release_acknowledgement(tmp_path, service.memory, report["report_id"],
                                       approver="owner", rationale="changed policy", current_policy=changed_policy)
    other = evaluate_release(tmp_path, service.memory, replace(target, target_id="other"), policy)
    freeze_assessment(tmp_path, service.memory, other)
    assert release_authorization(tmp_path, service.memory, other["report_id"],
                                 ack_id=ack["ack_id"])["authorization_state"] != "AUTHORIZED"
    (tmp_path / "main.tex").write_bytes(b"changed")
    assert release_authorization(tmp_path, service.memory, report["report_id"],
                                 ack_id=ack["ack_id"])["authorization_state"] == "BLOCKED"


@pytest.mark.parametrize("path", ["../outside", "/etc/hosts", "*.tex"])
def test_untrusted_paths_fail_closed(tmp_path, path):
    service, target, policy = setup_target(tmp_path)
    report = evaluate_release(tmp_path, service.memory, replace(target, manuscript_path=path), policy)
    assert report["overall_eligibility"] == "NOT_ELIGIBLE"


def test_symlink_source_fails_closed(tmp_path):
    service, target, policy = setup_target(tmp_path)
    (tmp_path / "alias.tex").symlink_to(tmp_path / "main.tex")
    report = evaluate_release(tmp_path, service.memory, replace(target, manuscript_path="alias.tex"), policy)
    assert report["overall_eligibility"] == "NOT_ELIGIBLE"


def canonical_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def bound_check(root, target, category="engineering_quality", **overrides):
    from paperforge.release_eligibility import EngineeringCheckResult

    log = f"{category}.log"
    record_path = f"{category}.json"
    (root / log).write_bytes(b"verification log")
    data = EngineeringCheckResult(check_id=category, category=category, command="verify",
                                  status="passed", exit_code=0, verifier="operator",
                                  log_artifact=log, record_path=record_path).to_dict()
    data.update(overrides)
    target_data = {k: v for k, v in target.to_dict().items() if k != "created_at"}
    (root / record_path).write_text(json.dumps({
        "schema": "paperforge.engineering-check/v1", "target_digest": canonical_digest(target_data),
        "result": data, "log_sha256": hashlib.sha256(b"verification log").hexdigest(),
    }))
    return data


@pytest.mark.parametrize("overrides", [{"currentness": "stale"}, {"exit_code": 1},
                                        {"failed": 1}, {"errors": 1}, {"record_path": ""}])
def test_engineering_false_pass_rejected(tmp_path, overrides):
    service, target, policy = setup_target(tmp_path)
    policy = replace(policy, required_gates=("engineering_quality",))
    check = bound_check(tmp_path, target)
    check.update(overrides)
    report = evaluate_release(tmp_path, service.memory, target, policy, engineering_checks=[check])
    assert report["overall_eligibility"] == "NOT_ELIGIBLE"


def test_engineering_and_environment_pass_then_log_change_stales(tmp_path):
    from paperforge.release_eligibility import freeze_assessment

    service, target, policy = setup_target(tmp_path)
    policy = replace(policy, required_gates=("source_integrity", "engineering_quality", "environment_readiness"))
    checks = [bound_check(tmp_path, target, category) for category in
              ("engineering_quality", "environment_readiness")]
    report = evaluate_release(tmp_path, service.memory, target, policy, engineering_checks=checks)
    assert report["overall_eligibility"] == "ELIGIBLE"
    freeze_assessment(tmp_path, service.memory, report)
    (tmp_path / "environment_readiness.log").write_bytes(b"different")
    assert not assessment_current(tmp_path, service.memory, report["report_id"])["currently_valid"]


def matrix_fixture(root, service, target, *, bind=True, optional=False):
    from paperforge.reviewer_matrix import (
        AcceptanceCriterion,
        EvidenceBinding,
        ReviewerAction,
        ReviewerComment,
        freeze_review_matrix,
    )

    target_data = {k: v for k, v in target.to_dict().items() if k != "created_at"}
    meta = {"target_digest": canonical_digest(target_data), "verifier": "owner", "verified_at": "t",
            "rationale": "reviewed", "verification_source": "human_explicit", "satisfies": ["k"]}
    actions = [ReviewerAction("a", "c", "required action", acceptance_criteria=("k",), status="verified")]
    criteria = [AcceptanceCriterion("k", "layout")]
    if optional:
        actions.append(ReviewerAction("optional", "c", "not done", acceptance_criteria=("opt",)))
        criteria.append(AcceptanceCriterion("opt", "layout", required=False))
    return freeze_review_matrix(root, service.memory,
                                comments=[ReviewerComment("c", "R", "Check layout")], actions=actions,
                                criteria=criteria, bindings=[EvidenceBinding(
                                    "b", "a", "human_verification", "verification", canonical_digest(meta),
                                    metadata=meta)] if bind else [])


def test_verified_matrix_requires_bound_verification_and_ignores_optional(tmp_path):
    service, target, policy = setup_target(tmp_path)
    policy = replace(policy, required_gates=("source_integrity", "reviewer_resolution"))
    for bind, expected in [(False, "NOT_ELIGIBLE"), (True, "ELIGIBLE")]:
        matrix = matrix_fixture(tmp_path, service, target, bind=bind, optional=True)
        report = evaluate_release(tmp_path, service.memory, target, policy,
                                  reviewer_projection={"matrix_id": matrix["matrix_id"]})
        assert report["overall_eligibility"] == expected


def pdf_fixture(root, target, *, state="passed", warning_class="", pdf_sha=None):
    page = {"page": 1, "image_hash": "a" * 64, "environment_role": "production"}
    human = {"page": 1, "render_sha256": "a" * 64, "pdf_sha256": target.final_pdf_sha256,
             "environment_role": "production", "review_source": "human_explicit", "reviewer": "owner",
             "reviewed_at": "t", "status": "passed"}
    payload = {"schema": "paperforge.pdf-qa-bundle/v1", "target_pdf_sha256": pdf_sha or target.final_pdf_sha256,
               "build_digest": target.build_digest, "preflight": {"page_count": 1},
               "render_manifest": [page], "human_reviews": [] if state == "needs_human" else [human],
               "layout_qa_state": state, "unresolved_critical": [], "page_coverage_complete": True,
               "layout_issues": [{"issue_id": "w", "category": warning_class, "severity": "warning", "status": "open"}]
               if warning_class else []}
    digest = canonical_digest(payload)
    bundle_id = f"qa-{digest[:16]}"
    payload.update(bundle_id=bundle_id, qa_digest=digest, created_at="t")
    folder = root / ".paperforge/pdf_qa" / bundle_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "qa.json").write_text(json.dumps(payload))
    (folder / "manifest.json").write_text(json.dumps({"bundle_id": bundle_id, "qa_digest": digest}))
    from paperforge.artifacts import ArtifactStore

    service = PaperForgeService(root)
    store = ArtifactStore(root, allowed_roots=(".paperforge/pdf_qa",), memory=service.memory)
    store.register(f".paperforge/pdf_qa/{bundle_id}/qa.json", kind="review")
    store.register(f".paperforge/pdf_qa/{bundle_id}/manifest.json", kind="manifest")
    return bundle_id


@pytest.mark.parametrize("state,warnings,allowed,expected", [
    ("passed", "", (), "ELIGIBLE"),
    ("needs_human", "", (), "NEEDS_HUMAN"),
    ("passed_with_warnings", "font", (), "NOT_ELIGIBLE"),
    ("passed_with_warnings", "font", ("font",), "ELIGIBLE"),
    ("passed_with_warnings", "font", ("other",), "NOT_ELIGIBLE"),
])
def test_pdf_policy_and_human_gate(tmp_path, state, warnings, allowed, expected):
    service, target, policy = setup_target(tmp_path)
    policy = replace(policy, required_gates=("source_integrity", "pdf_layout_qa"), allowed_warning_classes=allowed)
    bundle_id = pdf_fixture(tmp_path, target, state=state, warning_class=warnings)
    report = evaluate_release(tmp_path, service.memory, target, policy, pdf_bundle_ids=[bundle_id])
    assert report["overall_eligibility"] == expected


def test_pdf_from_another_target_rejected(tmp_path):
    service, target, policy = setup_target(tmp_path)
    policy = replace(policy, required_gates=("pdf_layout_qa",))
    bundle_id = pdf_fixture(tmp_path, target, pdf_sha="f" * 64)
    report = evaluate_release(tmp_path, service.memory, target, policy, pdf_bundle_ids=[bundle_id])
    assert report["overall_eligibility"] == "NOT_ELIGIBLE"


def test_evidence_caller_metadata_cannot_override_memory(tmp_path):
    service, target, policy = setup_target(tmp_path)
    policy = replace(policy, required_gates=("scientific_evidence",))
    eid = service.memory.add_evidence(evidence_type="TEST", excerpt="fixture",
                                      metadata={"environment_role": "fixture", "measurement_kind": "measured"})
    report = evaluate_release(tmp_path, service.memory, target, policy, evidence_refs=[{
        "evidence_id": eid, "environment_role": "scientific", "measurement_kind": "measured", "current": True}])
    assert report["overall_eligibility"] == "NOT_ELIGIBLE"


def test_domain_evidence_change_stales_report(tmp_path):
    from paperforge.release_eligibility import freeze_assessment

    service, target, policy = setup_target(tmp_path)
    eid = service.memory.add_evidence(evidence_type="TEST", excerpt="test observation",
                                      metadata={"environment_role": "scientific", "measurement_kind": "measured"})
    policy = replace(policy, required_gates=("scientific_evidence",))
    report = evaluate_release(tmp_path, service.memory, target, policy, evidence_refs=[{"evidence_id": eid}])
    assert report["overall_eligibility"] == "ELIGIBLE"
    freeze_assessment(tmp_path, service.memory, report)
    with service.memory.connect() as db:
        db.execute("DELETE FROM evidence WHERE id=?", (eid,))
    assert not assessment_current(tmp_path, service.memory, report["report_id"])["currently_valid"]


def test_default_policy_all_gates_then_external_source_change(tmp_path):
    from paperforge.evidence_semantics import EvidenceSemantics
    from paperforge.external_import import (
        approve_import,
        materialize_import,
        stage_import,
        validate_import,
    )
    from paperforge.release_eligibility import freeze_assessment
    from paperforge.scientific_review import ScientificClaim, freeze_review, review_claim

    service, target, _ = setup_target(tmp_path)
    (tmp_path / "observation.json").write_text('{"latency_ms": 22.4}')
    record = stage_import(tmp_path, service.memory,
                          semantics=EvidenceSemantics.from_metadata({
                              "measurement_kind": "measured", "acquisition_origin": "external_import",
                              "environment_role": "scientific"}),
                          subject={"model_id": "m", "dataset_id": "d", "seed": 0},
                          execution={"device": "test-device", "runtime": "test-runtime", "timing_scope": "inference_only"},
                          source_files=["observation.json"])
    validation = validate_import(tmp_path, service.memory, record.import_id)
    approve_import(tmp_path, service.memory, record.import_id, approver="test-owner",
                   acknowledged_checks=[c["check_id"] for c in validation["checks"] if c["status"] == "needs_human"],
                   rationale="isolated test fixture")
    evidence = materialize_import(tmp_path, service.memory, record.import_id)
    claim = ScientificClaim("claim", "A descriptive value.", "descriptive_metric", fact_ids=("f",))
    review = freeze_review(tmp_path, service.memory, claims=[claim], results=review_claim(claim, facts={"f": {}}),
                           fact_artifact_hashes={"main.tex": target.manuscript_sha256})
    matrix = matrix_fixture(tmp_path, service, target, optional=True)
    bundle_id = pdf_fixture(tmp_path, target)
    checks = [bound_check(tmp_path, target, category) for category in
              ("engineering_quality", "environment_readiness")]
    inputs = {"evidence_refs": [{"evidence_id": e} for e in evidence["evidence_ids"]],
              "review_ids": [review["review_id"]], "reviewer_projection": {"matrix_id": matrix["matrix_id"]},
              "pdf_bundle_ids": [bundle_id], "engineering_checks": checks}
    with service.memory.connect() as db:
        before = list(db.iterdump())
    report = evaluate_release(tmp_path, service.memory, target, ReleasePolicy(), **inputs)
    assert report["overall_eligibility"] == "ELIGIBLE", report["gates"]
    with service.memory.connect() as db:
        assert list(db.iterdump()) == before
    freeze_assessment(tmp_path, service.memory, report)
    assert assessment_current(tmp_path, service.memory, report["report_id"])["currently_valid"]
    assert not assessment_current(tmp_path, service.memory, report["report_id"],
                                  current_policy=ReleasePolicy(policy_id="replacement"))["currently_valid"]
    source = tmp_path / ".paperforge/imports" / record.import_id / "sources/observation.json"
    original = source.read_bytes()
    source.unlink()
    source.symlink_to(tmp_path / "observation.json")
    assert evaluate_release(tmp_path, service.memory, target, ReleasePolicy(), **inputs)["overall_eligibility"] == "NOT_ELIGIBLE"
    source.unlink()
    source.write_bytes(original)
    source.write_text('{"latency_ms": 99}')
    assert not assessment_current(tmp_path, service.memory, report["report_id"])["currently_valid"]
    assert evaluate_release(tmp_path, service.memory, target, ReleasePolicy(), **inputs)["overall_eligibility"] == "NOT_ELIGIBLE"


def test_unified_release_summary_is_only_a_projection():
    from paperforge.run_status import build_unified_status

    status = build_unified_status({"id": "run", "status": "RUNNING"},
                                   {"status": "completed", "exit_code": 0},
                                   release_assessment={"report_id": "r", "overall_eligibility": "ELIGIBLE",
                                                       "authorization_state": "AUTHORIZED", "release_state": "NOT_RELEASED"})
    assert status.workflow_state == "RUNNING"
    assert status.release_assessment["authoritative"] is False
    assert status.release_assessment["release_state"] == "NOT_RELEASED"


@pytest.mark.parametrize("criterion_type", ["layout", "manuscript_clarification", "unknown_future_criterion"])
def test_measurement_cannot_satisfy_unrelated_reviewer_criterion(tmp_path, criterion_type):
    from paperforge.reviewer_matrix import (
        AcceptanceCriterion,
        EvidenceBinding,
        ReviewerAction,
        ReviewerComment,
        freeze_review_matrix,
    )

    service, target, policy = setup_target(tmp_path)
    eid = service.memory.add_evidence(evidence_type="TEST", excerpt="measurement",
                                      metadata={"measurement_kind": "measured", "environment_role": "scientific"})
    matrix = freeze_review_matrix(tmp_path, service.memory,
        comments=[ReviewerComment("c", "R", "Check")],
        actions=[ReviewerAction("a", "c", "done", acceptance_criteria=("k",), status="verified")],
        criteria=[AcceptanceCriterion("k", criterion_type)],
        bindings=[EvidenceBinding("b", "a", "imported_evidence", eid,
                                  hashlib.sha256(b"measurement").hexdigest(), metadata={"satisfies": ["k"]})])
    report = evaluate_release(tmp_path, service.memory, target,
                              replace(policy, required_gates=("reviewer_resolution",)),
                              evidence_refs=[{"evidence_id": eid}], reviewer_projection={"matrix_id": matrix["matrix_id"]})
    assert report["overall_eligibility"] == "NOT_ELIGIBLE"
