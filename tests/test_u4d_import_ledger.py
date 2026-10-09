"""Isolated synthetic transport fixtures, never real scientific evidence."""

import hashlib
import json

import pytest

from paperforge.api import PaperForgeService
from paperforge.evidence_semantics import EvidenceSemantics
from paperforge.external_import import (
    ApprovalError,
    approve_import,
    materialize_import,
    stage_import,
    validate_import,
)
from paperforge.result_ledger import LedgerError, ResultLedgerQuery, freeze_ledger, verify_ledger


def imported_metric(root, *, role="scientific", kind="measured", origin="external_import", payload=None):
    # Scientific-role API branch is exercised only inside pytest's disposable DB.
    # The explicit approval below is a test double, not an operator attestation.
    service = PaperForgeService(root)
    payload = payload or {"schema": "paperforge.metric-json/v1", "metric": "latency_ms",
                          "value": "12.500", "unit": "ms"}
    raw = (json.dumps(payload, sort_keys=True) + "\n").encode()
    (root / "fixture.json").write_bytes(raw)
    record = stage_import(root, service.memory,
                          semantics=EvidenceSemantics(kind, origin, role),
                          subject={"model_id": "TEST_ONLY", "dataset_id": "TEST_ONLY", "seed": 1},
                          execution={"device": "TEST_ONLY", "runtime": "TEST_ONLY",
                                     "timing_scope": "inference_only"},
                          source_files=["fixture.json"])
    validation = validate_import(root, service.memory, record.import_id)
    if role != "scientific" or kind == "transcribed":
        return service, record, validation, raw
    approve_import(root, service.memory, record.import_id, approver="TEST_ONLY",
                   rationale="Synthetic API transport test; not physical evidence",
                   acknowledged_checks=[c["check_id"] for c in validation["checks"]
                                        if c["status"] == "needs_human"])
    materialized = materialize_import(root, service.memory, record.import_id,
                                     metric="latency_ms",
                                     metric_provenance={"acquisition_method": "timer_samples"})
    return service, record, materialized, raw


def test_imported_metric_reaches_ledger_without_minting_evidence(tmp_path):
    service, record, materialized, raw = imported_metric(tmp_path)
    with service.memory.connect() as db:
        before = [tuple(r) for r in db.execute("SELECT * FROM evidence")]
    ledger = freeze_ledger(tmp_path, service.memory, ResultLedgerQuery(
        evidence_ids=tuple(materialized["evidence_ids"])))
    assert len(ledger["snapshots"]) == 1
    snapshot = ledger["snapshots"][0]
    assert snapshot["value_str"] == "12.5"
    assert snapshot["source_lexical_value"] == "12.500"
    assert snapshot["evidence_id"] == materialized["evidence_ids"][0]
    assert snapshot["import_id"] == record.import_id
    assert snapshot["run_identity"]["model_id"] == "TEST_ONLY"
    assert snapshot["adapter_id"] == "scalar-metric-json"
    assert snapshot["source_file_hash"] == hashlib.sha256(raw).hexdigest()
    assert snapshot["source_locator"]["pointer"].endswith("#/value")
    assert verify_ledger(tmp_path, service.memory, ledger["ledger_id"])["eligible_as_release_input"]
    with service.memory.connect() as db:
        assert [tuple(r) for r in db.execute("SELECT * FROM evidence")] == before


def test_imported_ledger_verifies_in_read_only_release_context(tmp_path):
    from paperforge.release_eligibility import _ReadOnlyMemory

    service, _, materialized, _ = imported_metric(tmp_path)
    ledger = freeze_ledger(tmp_path, service.memory, ResultLedgerQuery(
        evidence_ids=tuple(materialized["evidence_ids"])))
    with service.memory.connect() as db:
        before = list(db.iterdump())
    status = verify_ledger(tmp_path, _ReadOnlyMemory(service.memory), ledger["ledger_id"])
    assert status["eligible_as_release_input"]
    with service.memory.connect() as db:
        assert list(db.iterdump()) == before


@pytest.mark.parametrize("relative", [
    "sources\\fixture.json", "sources/../fixture.json", "sources/./fixture.json",
    "sources//fixture.json", "/sources/fixture.json", "sources/file:stream",
])
def test_import_source_path_requires_canonical_portable_relative_path(tmp_path, relative):
    from paperforge.result_ledger import _imported_metric_snapshots

    service, _, materialized, _ = imported_metric(tmp_path)
    eid = materialized["evidence_ids"][0]
    with service.memory.connect() as db:
        row = dict(db.execute("SELECT * FROM evidence WHERE id=?", (eid,)).fetchone())
    metadata = json.loads(row["metadata_json"])
    metadata["source_relpath"] = relative
    with pytest.raises(LedgerError, match="invalid imported metric source path"):
        _imported_metric_snapshots(tmp_path, service.memory, eid, row, metadata,
                                   ResultLedgerQuery(evidence_ids=(eid,)))


@pytest.mark.parametrize("role", ["fixture", "synthetic_test"])
def test_fixture_import_does_not_receive_scientific_approval(tmp_path, role):
    service, record, validation, _ = imported_metric(tmp_path, role=role)
    assert validation["overall_status"] == "failed"
    with pytest.raises(ApprovalError):
        approve_import(tmp_path, service.memory, record.import_id,
                       approver="TEST_ONLY", rationale="must not bypass")
    with pytest.raises(ApprovalError):
        materialize_import(tmp_path, service.memory, record.import_id)


def test_manuscript_transcription_cannot_bootstrap_scientific_evidence(tmp_path):
    service, record, validation, _ = imported_metric(
        tmp_path, kind="transcribed", origin="manuscript_existing")
    assert validation["overall_status"] == "failed"
    with pytest.raises(ApprovalError):
        approve_import(tmp_path, service.memory, record.import_id,
                       approver="TEST_ONLY", rationale="must not self-bootstrap")
    with pytest.raises(ApprovalError):
        materialize_import(tmp_path, service.memory, record.import_id)
    with service.memory.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0


def test_import_ledger_refuses_changed_source(tmp_path):
    service, record, materialized, _ = imported_metric(tmp_path)
    source = tmp_path / ".paperforge/imports" / record.import_id / "sources/fixture.json"
    source.write_bytes(b'{"value":"999"}')
    with pytest.raises(LedgerError, match="current|integrity|approval"):
        freeze_ledger(tmp_path, service.memory, ResultLedgerQuery(
            evidence_ids=tuple(materialized["evidence_ids"])))


def test_import_ledger_rejects_payload_semantic_override(tmp_path):
    service, _, materialized, _ = imported_metric(tmp_path, payload={
        "schema": "paperforge.metric-json/v1", "metric": "latency_ms",
        "value": "12.5", "unit": "ms", "measurement_kind": "simulated"})
    with pytest.raises(ValueError, match="SEMANTICS_MISMATCH"):
        freeze_ledger(tmp_path, service.memory, ResultLedgerQuery(
            evidence_ids=tuple(materialized["evidence_ids"])))


def test_imported_ledger_becomes_stale_when_source_changes(tmp_path):
    service, record, materialized, _ = imported_metric(tmp_path)
    ledger = freeze_ledger(tmp_path, service.memory, ResultLedgerQuery(
        evidence_ids=tuple(materialized["evidence_ids"])))
    source = tmp_path / ".paperforge/imports" / record.import_id / "sources/fixture.json"
    source.write_bytes(b"changed")
    status = verify_ledger(tmp_path, service.memory, ledger["ledger_id"])
    assert status["historical_integrity"] == "valid"
    assert status["source_currentness"] == "stale"
    assert not status["eligible_as_release_input"]


@pytest.mark.parametrize("metric,unit", [("fps", "fps"), ("energy", "J")])
def test_import_adapter_cannot_launder_ambiguous_measured_metrics(tmp_path, metric, unit):
    service, _, materialized, _ = imported_metric(tmp_path, payload={
        "schema": "paperforge.metric-json/v1", "metric": metric, "value": "12.5", "unit": unit})
    with pytest.raises(ApprovalError, match="acquisition_method"):
        freeze_ledger(tmp_path, service.memory, ResultLedgerQuery(
            evidence_ids=tuple(materialized["evidence_ids"])))


def test_import_ledger_does_not_accept_inline_metadata_metric_override(tmp_path):
    service, _, materialized, _ = imported_metric(tmp_path)
    eid = materialized["evidence_ids"][0]
    with service.memory.connect() as db:
        metadata = json.loads(db.execute("SELECT metadata_json FROM evidence WHERE id=?", (eid,)).fetchone()[0])
        metadata.update(metric_name="latency_ms", unit="ms", value_str="999")
        db.execute("UPDATE evidence SET metadata_json=? WHERE id=?", (json.dumps(metadata), eid))
    ledger = freeze_ledger(tmp_path, service.memory, ResultLedgerQuery(evidence_ids=(eid,)))
    assert ledger["snapshots"][0]["value_str"] == "12.5"


def test_import_ledger_rejects_semantics_changed_after_approval(tmp_path):
    service, _, materialized, _ = imported_metric(tmp_path, payload={
        "schema": "paperforge.metric-json/v1", "metric": "latency_ms",
        "value": "12.5", "unit": "ms", "measurement_kind": "simulated"})
    eid = materialized["evidence_ids"][0]
    with service.memory.connect() as db:
        metadata = json.loads(db.execute("SELECT metadata_json FROM evidence WHERE id=?", (eid,)).fetchone()[0])
        metadata["measurement_kind"] = "simulated"
        db.execute("UPDATE evidence SET metadata_json=? WHERE id=?", (json.dumps(metadata), eid))
    with pytest.raises(LedgerError, match="approved semantics"):
        freeze_ledger(tmp_path, service.memory, ResultLedgerQuery(evidence_ids=(eid,)))


@pytest.mark.parametrize("damage", ["semantics", "deleted_import", "changed_import", "excerpt"])
def test_frozen_import_ledger_currentness_rechecks_evidence_semantics(tmp_path, damage):
    service, _, materialized, _ = imported_metric(tmp_path)
    eid = materialized["evidence_ids"][0]
    ledger = freeze_ledger(tmp_path, service.memory, ResultLedgerQuery(evidence_ids=(eid,)))
    with service.memory.connect() as db:
        metadata = json.loads(db.execute("SELECT metadata_json FROM evidence WHERE id=?", (eid,)).fetchone()[0])
        if damage == "semantics":
            metadata["environment_role"] = "fixture"
        elif damage == "deleted_import":
            del metadata["import_id"]
        elif damage == "changed_import":
            metadata["import_id"] = "another-import"
        else:
            db.execute("UPDATE evidence SET excerpt='tampered' WHERE id=?", (eid,))
        db.execute("UPDATE evidence SET metadata_json=? WHERE id=?", (json.dumps(metadata), eid))
    status = verify_ledger(tmp_path, service.memory, ledger["ledger_id"])
    assert status["historical_integrity"] == "valid"
    assert status["source_currentness"] == "stale"
    assert not status["eligible_as_release_input"]
