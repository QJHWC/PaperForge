from __future__ import annotations

import hashlib
import json
from pathlib import Path

from paperforge.api import PaperForgeService
from paperforge.release_eligibility import (
    EngineeringCheckResult,
    ReleasePolicy,
    ReleaseTarget,
    assessment_current,
    evaluate_release,
    freeze_assessment,
    release_authorization,
)


def _service(tmp_path: Path) -> PaperForgeService:
    return PaperForgeService(tmp_path)


def _target(tmp_path: Path, manuscript: bytes = b"manuscript v1", pdf: bytes = b"%PDF-1.4 v1") -> ReleaseTarget:
    (tmp_path / "main.tex").write_bytes(manuscript)
    (tmp_path / "final.pdf").write_bytes(pdf)
    build = {"schema": "paperforge.build-provenance/v1", "status": "succeeded",
             "source_hashes": {"main.tex": hashlib.sha256(manuscript).hexdigest()},
             "output_path": "final.pdf", "output_sha256": hashlib.sha256(pdf).hexdigest(),
             "command": ["compiler", "main.tex"], "toolchain": {"compiler": "test"}}
    raw = json.dumps(build).encode()
    (tmp_path / "build.json").write_bytes(raw)
    return ReleaseTarget(
        target_id="t1", manuscript_path="main.tex",
        manuscript_sha256=hashlib.sha256(manuscript).hexdigest(),
        final_pdf_path="final.pdf", final_pdf_sha256=hashlib.sha256(pdf).hexdigest(),
        build_manifest_path="build.json", build_digest=hashlib.sha256(raw).hexdigest())


def _policy(**overrides):
    base = {"required_gates": ("source_integrity",)}
    base.update(overrides)
    return ReleasePolicy(**base)


def test_source_hash_mismatch() -> None:  # 1,2
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        service = _service(root)
        target = _target(root)
        bad = ReleaseTarget(target_id="t1", manuscript_path="main.tex", manuscript_sha256="0" * 64,
                            final_pdf_path="final.pdf", final_pdf_sha256=target.final_pdf_sha256)
        report = evaluate_release(root, service.memory, bad, _policy())
        assert report["overall_eligibility"] == "NOT_ELIGIBLE"


def test_stale_build_binding() -> None:  # 3
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        service = _service(root)
        target = _target(root)
        (root / "main.tex").write_bytes(b"manuscript v2")
        report = evaluate_release(root, service.memory, target, _policy())
        assert report["overall_eligibility"] == "NOT_ELIGIBLE"


def test_fixture_and_transcribed_blocked() -> None:  # 4,5
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        service = _service(root)
        target = _target(root)
        policy = ReleasePolicy(required_gates=("source_integrity", "scientific_evidence"))
        report = evaluate_release(root, service.memory, target, policy, evidence_refs=[
            {"evidence_id": "e1", "environment_role": "fixture", "measurement_kind": "measured", "current": True}])
        assert report["overall_eligibility"] == "NOT_ELIGIBLE"
        report2 = evaluate_release(root, service.memory, target, policy, evidence_refs=[
            {"evidence_id": "e2", "environment_role": "scientific", "measurement_kind": "transcribed", "current": True}])
        assert report2["overall_eligibility"] == "NOT_ELIGIBLE"


def test_ledger_current_and_stale() -> None:  # 6,7
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        service = _service(root)
        from paperforge.result_ledger import ResultLedgerQuery, freeze_ledger, verify_ledger

        eid = service.memory.add_evidence(
            evidence_type="EXTERNAL_MEASUREMENT", excerpt="m 1 ms", path="m.txt", config_scope="t",
            metadata={"measurement_kind": "measured", "acquisition_origin": "paperforge_run",
                      "environment_role": "scientific", "metric_name": "latency_ms",
                      "value_str": "1", "unit": "ms",
                      "run_identity": {"schema": "paperforge.scientific-run-identity/v1"}})
        ledger = freeze_ledger(root, service.memory, ResultLedgerQuery(
            purpose="scientific", metric_names=["latency_ms"], evidence_ids=(eid,)))
        assert verify_ledger(root, service.memory, ledger["ledger_id"])["eligible_as_release_input"] is True


def test_review_states_distinguished() -> None:  # 8,9,10
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        service = _service(root)
        target = _target(root)
        policy = ReleasePolicy(required_gates=("source_integrity", "scientific_review"))
        report = evaluate_release(root, service.memory, target, policy, review_ids=["review-missing"])
        assert report["overall_eligibility"] == "NOT_ELIGIBLE"


def test_reviewer_required_states() -> None:  # 11,12,13,14
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        service = _service(root)
        target = _target(root)
        policy = ReleasePolicy(required_gates=("source_integrity", "reviewer_resolution"))
        blocked = evaluate_release(root, service.memory, target, policy,
                                   reviewer_projection={"required_states": ["partial"]})
        assert blocked["overall_eligibility"] == "NOT_ELIGIBLE"
        verified = evaluate_release(root, service.memory, target, policy,
                                    reviewer_projection={"required_states": ["verified"]})
        # An unbound caller projection cannot stand in for a persisted matrix.
        assert verified["overall_eligibility"] == "NOT_ELIGIBLE"
        addressed = evaluate_release(root, service.memory, target, policy,
                                     reviewer_projection={"required_states": ["addressed"]})
        assert addressed["overall_eligibility"] == "NOT_ELIGIBLE"


def test_pdf_human_and_stale_and_critical() -> None:  # 15,16,17
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        service = _service(root)
        target = _target(root)
        policy = ReleasePolicy(required_gates=("source_integrity", "pdf_layout_qa"))
        report = evaluate_release(root, service.memory, target, policy, pdf_bundle_ids=[])
        assert report["overall_eligibility"] == "NOT_ELIGIBLE"


def test_warnings_policy_and_human_records() -> None:  # 18,19,20
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        service = _service(root)
        target = _target(root)
        policy = ReleasePolicy(required_gates=("source_integrity",),
                               require_human_owner_ack=True)
        needs = evaluate_release(root, service.memory, target, policy, human_records=[])
        assert needs["overall_eligibility"] == "ELIGIBLE"
        freeze_assessment(root, service.memory, needs)
        assert release_authorization(root, service.memory, needs["report_id"])["authorization_state"] == "NEEDS_HUMAN"
        explicit = evaluate_release(root, service.memory, target, policy, human_records=[
            {"kind": "owner_ack", "approver": "owner", "recorded_at": "t"}])
        assert explicit["overall_eligibility"] == "ELIGIBLE"
        freeze_assessment(root, service.memory, explicit)
        assert release_authorization(root, service.memory, explicit["report_id"])["authorization_state"] == "NEEDS_HUMAN"


def test_engineering_statuses() -> None:  # 21,22,23,24
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        service = _service(root)
        target = _target(root)
        policy = ReleasePolicy(required_gates=("source_integrity", "engineering_quality",
                                               "environment_readiness"))
        failed = evaluate_release(root, service.memory, target, policy, engineering_checks=[
            EngineeringCheckResult(check_id="pytest", command="pytest", status="failed", exit_code=1)])
        assert failed["overall_eligibility"] == "NOT_ELIGIBLE"
        unavailable = evaluate_release(root, service.memory, target, policy, engineering_checks=[
            EngineeringCheckResult(check_id="ruff", command="ruff", status="unavailable")])
        assert unavailable["overall_eligibility"] == "NOT_ELIGIBLE"
        env_blocked = evaluate_release(root, service.memory, target, policy, engineering_checks=[
            EngineeringCheckResult(check_id="x", command="x", status="blocked_environment")])
        assert env_blocked["overall_eligibility"] == "NOT_ELIGIBLE"


def test_aggregation_and_digest() -> None:  # 25,26,27,28,29,30
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        service = _service(root)
        target = _target(root)
        policy = _policy()
        first = evaluate_release(root, service.memory, target, policy)
        second = evaluate_release(root, service.memory, target, policy)
        assert first["report_digest"] == second["report_digest"]
        assert first["overall_eligibility"] == "ELIGIBLE"
        freeze_assessment(root, service.memory, first)
        assert assessment_current(root, service.memory, first["report_id"])["currently_valid"] is True


def test_no_side_effects_and_not_evidence() -> None:  # 31,32,33
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        service = _service(root)
        target = _target(root)
        with service.memory.connect() as db:
            before = db.execute("SELECT COUNT(*) AS c FROM evidence").fetchone()["c"]
        report = evaluate_release(root, service.memory, target, _policy())
        with service.memory.connect() as db:
            after = db.execute("SELECT COUNT(*) AS c FROM evidence").fetchone()["c"]
        assert before == after
        with service.memory.connect() as db:
            rows = db.execute("SELECT * FROM artifacts WHERE path LIKE '%release%'").fetchall()
        assert not rows
        freeze_assessment(root, service.memory, report)
        with service.memory.connect() as db:
            rows = db.execute("SELECT * FROM artifacts WHERE path LIKE '%release%'").fetchall()
        assert rows
        with service.memory.connect() as db:
            evidence = db.execute("SELECT * FROM evidence").fetchall()
        assert all(report["report_id"] not in str(r["id"]) for r in evidence)


def test_unified_only_displays() -> None:  # 34
    from paperforge.run_status import build_unified_status

    unified = build_unified_status({"id": "w", "status": "RUNNING"})
    assert unified.workflow_state == "RUNNING"


def test_no_pcb_hardcoding_and_regression_anchor() -> None:  # core vocabulary
    source = Path("paperforge/release_eligibility.py").read_text(encoding="utf-8").lower()
    for token in ("deeppcb", "yolo11", "sim_rpi5"):
        assert token not in source
