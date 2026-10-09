"""Synthetic end-to-end transport; never a real-paper release certificate."""

import hashlib
import json

import pymupdf

from paperforge.grounded_narrative import (
    GroundedNarrativeSpec,
    NarrativeSentence,
    render_narrative,
    validate_narrative,
)
from paperforge.pdf_qa import PDFQASpec, run_pdf_qa, verify_pdf_qa
from paperforge.publication_artifacts import (
    FactSummarySpec,
    FigureSpec,
    TableSpec,
    generate_facts,
    generate_figure,
    generate_table,
    verify_publication_bundle,
)
from paperforge.release_eligibility import (
    ReleasePolicy,
    ReleaseTarget,
    assessment_current,
    evaluate_release,
    freeze_assessment,
    release_authorization,
)
from paperforge.result_ledger import ResultLedgerQuery, freeze_ledger, verify_ledger
from paperforge.reviewer_matrix import (
    AcceptanceCriterion,
    EvidenceBinding,
    ReviewerAction,
    ReviewerComment,
    freeze_review_matrix,
    verify_review_matrix,
)
from paperforge.scientific_review import (
    ScientificClaim,
    freeze_review,
    review_claim,
    verify_scientific_review,
)
from tests.test_u4d_import_ledger import imported_metric


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_final_evidence_to_release_status_smoke(tmp_path):
    service, record, materialized, raw = imported_metric(tmp_path)
    ledger = freeze_ledger(tmp_path, service.memory, ResultLedgerQuery(
        evidence_ids=tuple(materialized["evidence_ids"])))
    assert len(ledger["snapshots"]) == 1
    assert verify_ledger(tmp_path, service.memory, ledger["ledger_id"])["eligible_as_release_input"]
    fact = generate_facts(tmp_path, service.memory, FactSummarySpec(
        ledger_id=ledger["ledger_id"], row_id=ledger["detail_rows"][0]["row_id"]))
    table = generate_table(tmp_path, service.memory, TableSpec(
        ledger_id=ledger["ledger_id"], columns=("latency_ms",)))
    figure = generate_figure(tmp_path, service.memory, FigureSpec(
        ledger_id=ledger["ledger_id"], x_metric="latency_ms", y_metric="latency_ms"))
    # This deliberately dependent preview is not independent scientific evidence.
    for kind, artifact in (("facts", fact), ("tables", table), ("figures", figure)):
        aid = artifact[{"facts": "fact_id", "tables": "table_id", "figures": "figure_id"}[kind]]
        checked = verify_publication_bundle([artifact["manifest"]])
        assert checked["artifact_integrity"]
        for name, digest in artifact["manifest"]["output_hashes"].items():
            if name != "manifest.json":
                assert _sha(tmp_path / "artifacts" / kind / aid / name) == digest
        assert artifact["manifest"]["publication_ready"] is False
    sentence = NarrativeSentence("s1", "The test measured latency was 12.5 ms.",
                                 fact_ids=(fact["fact_id"],))
    spec = GroundedNarrativeSpec(allowed_fact_ids=(fact["fact_id"],))
    paragraphs = [[sentence]]
    validated = validate_narrative(spec, paragraphs, facts=[fact["fact"]])
    assert validated["overall_status"] == "passed"
    rendered = render_narrative(paragraphs)
    claim = ScientificClaim.from_narrative_sentence(sentence.to_dict())
    review = freeze_review(tmp_path, service.memory, claims=[claim],
                           results=review_claim(claim, facts={fact["fact_id"]: fact["fact"]}),
                           ledger_bindings=[{"ledger_id": ledger["ledger_id"],
                                             "ledger_digest": ledger["ledger_digest"]}])
    assert verify_scientific_review(tmp_path, service.memory, review["review_id"])["currently_valid_as_review_input"]
    matrix = freeze_review_matrix(tmp_path, service.memory,
        comments=[ReviewerComment("TEST_R1", "synthetic test", "Check the test metric source.")],
        actions=[ReviewerAction("a1", "TEST_R1", "Trace the generated fact",
                                acceptance_criteria=("c1",))],
        criteria=[AcceptanceCriterion("c1", "hardware_measurement")],
        bindings=[EvidenceBinding("b1", "a1", "fact", fact["fact_id"])],
        review_ids=[review["review_id"]])
    assert verify_review_matrix(tmp_path, service.memory, matrix["matrix_id"])["historical_integrity"] == "valid"

    # A real PDF build from this fixture source, not a forged PDF header.
    source = tmp_path / "fixture-source.txt"
    source.write_text(rendered, encoding="utf-8")
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((72, 72), source.read_text(), fontsize=12)
        document.save(tmp_path / "fixture.pdf")
    build = {"schema": "paperforge.build-provenance/v1", "status": "succeeded",
             "source_hashes": {source.name: _sha(source)},
             "output_path": "fixture.pdf", "output_sha256": _sha(tmp_path / "fixture.pdf"),
             "command": ["pytest", "tests/test_u4d_final_smoke.py"],
             "toolchain": {"pymupdf": pymupdf.VersionBind}, "environment_role": "synthetic_test"}
    (tmp_path / "build.json").write_text(json.dumps(build, sort_keys=True))
    pdf_qa = run_pdf_qa(tmp_path, service.memory, PDFQASpec(
        target_pdf_path="fixture.pdf", target_pdf_sha256=build["output_sha256"],
        build_digest=_sha(tmp_path / "build.json")))
    assert len(pdf_qa["render_manifest"]) == 1
    assert all(r["renderer"].startswith("poppler-pdftoppm/") for r in pdf_qa["render_manifest"])
    assert pdf_qa["human_reviews"] == []
    assert pdf_qa["layout_qa_state"] != "passed"
    assert verify_pdf_qa(tmp_path, service.memory, pdf_qa["bundle_id"])["historical_integrity"] == "valid"
    target = ReleaseTarget("SYNTHETIC_TEST_ONLY", manuscript_path=source.name,
                           manuscript_sha256=_sha(source), final_pdf_path="fixture.pdf",
                           final_pdf_sha256=build["output_sha256"],
                           build_manifest_path="build.json", build_digest=_sha(tmp_path / "build.json"))
    with service.memory.connect() as db:
        before = list(db.iterdump())
    report = evaluate_release(tmp_path, service.memory, target, ReleasePolicy(),
        evidence_refs=[{"evidence_id": eid} for eid in materialized["evidence_ids"]],
        ledger_ids=[ledger["ledger_id"]], review_ids=[review["review_id"]],
        reviewer_projection={"matrix_id": matrix["matrix_id"]}, pdf_bundle_ids=[pdf_qa["bundle_id"]])
    assert report["overall_eligibility"] != "ELIGIBLE"
    with service.memory.connect() as db:
        assert list(db.iterdump()) == before
    freeze_assessment(tmp_path, service.memory, report)
    assert assessment_current(tmp_path, service.memory, report["report_id"])["currently_valid"]
    assert release_authorization(tmp_path, service.memory, report["report_id"])["release_state"] == "NOT_RELEASED"
    handle = service.run(profile="writing-only", inputs={"title": "Synthetic test", "topic": "Test"})
    status = service.unified_status(handle.run_id)
    assert status["unified"]["process_state"] == "UNKNOWN_NOT_ATTACHED"
    assert source.read_text() == rendered
    assert (tmp_path / "fixture.json").read_bytes() == raw
    result = {"schema": "paperforge.final-smoke-report/v1", "status": "PASSED",
              "environment_role": "synthetic_test", "import_id": record.import_id,
              "ledger_id": ledger["ledger_id"], "fact_id": fact["fact_id"],
              "table_id": table["table_id"], "figure_id": figure["figure_id"],
              "review_id": review["review_id"], "matrix_id": matrix["matrix_id"],
              "pdf_qa_id": pdf_qa["bundle_id"], "release_report_id": report["report_id"],
              "human_page_reviews_created": 0, "released": False,
              "note": "Import approval is an explicit fixture-only test double, not a real attestation."}
    (tmp_path / "FinalSmokeReport.json").write_text(json.dumps(result, sort_keys=True, indent=2))
