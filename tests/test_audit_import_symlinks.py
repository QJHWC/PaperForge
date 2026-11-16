import json

import pytest
from test_u4d_import_ledger import imported_metric

from paperforge.api import PaperForgeService
from paperforge.evidence_semantics import EvidenceSemantics
from paperforge.external_import import (
    approval_valid,
    approve_import,
    materialize_import,
    reconcile_imports,
    stage_import,
    validate_import,
)


def test_same_byte_symlink_cannot_keep_import_approval_or_clean_recovery(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    service, record, _, raw = imported_metric(root)
    base = root / ".paperforge/imports" / record.import_id
    manifest = json.loads((base / "manifest.json").read_bytes())
    source = base / manifest["sources"][0]["path"]
    outside = tmp_path / "outside.json"
    outside.write_bytes(raw)
    source.unlink()
    source.symlink_to(outside)
    with service.memory.connect() as db:
        before = list(db.iterdump())
    valid, _ = approval_valid(root, service.memory, record.import_id)
    assert valid is False
    report = reconcile_imports(root, service.memory, repair=True)
    assert report["recovery_outcome"] == "MANUAL_INTERVENTION_REQUIRED"
    assert report["repaired"] is False
    assert source.is_symlink()
    assert outside.read_bytes() == raw
    with service.memory.connect() as db:
        assert list(db.iterdump()) == before


@pytest.mark.parametrize("digest_case", ["lower", "upper", "mixed"])
def test_reconciliation_accepts_declared_digest_case_but_rejects_changed_bytes(tmp_path, digest_case):
    service = PaperForgeService(tmp_path)
    raw = b'{"schema":"paperforge.metric-json/v1","metric":"latency_ms","value":"12.5","unit":"ms"}\n'
    (tmp_path / "fixture.json").write_bytes(raw)
    record = stage_import(
        tmp_path, service.memory, semantics=EvidenceSemantics("measured", "external_import", "scientific"),
        subject={"model_id": "TEST_ONLY", "dataset_id": "TEST_ONLY", "seed": 1},
        execution={"device": "TEST_ONLY", "runtime": "TEST_ONLY", "timing_scope": "inference_only"},
        source_files=["fixture.json"])
    base = tmp_path / ".paperforge/imports" / record.import_id
    manifest_path = base / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    digest = manifest["sources"][0]["sha256"]
    manifest["sources"][0]["sha256"] = (
        digest.upper() if digest_case == "upper" else
        "".join(char.upper() if index % 2 else char for index, char in enumerate(digest))
        if digest_case == "mixed" else digest)
    manifest_path.write_bytes(json.dumps(manifest, sort_keys=True).encode("utf-8"))
    validation = validate_import(tmp_path, service.memory, record.import_id)
    assert validation["overall_status"] != "failed"
    approve_import(
        tmp_path, service.memory, record.import_id, approver="TEST_ONLY",
        rationale="Synthetic hash-case regression; not physical evidence",
        acknowledged_checks=[check["check_id"] for check in validation["checks"]
                             if check["status"] == "needs_human"])
    materialize_import(tmp_path, service.memory, record.import_id, metric="latency_ms",
                       metric_provenance={"acquisition_method": "timer_samples"})
    assert approval_valid(tmp_path, service.memory, record.import_id)[0]
    with service.memory.connect() as db:
        before = list(db.iterdump())
    assert reconcile_imports(tmp_path, service.memory)["recovery_outcome"] == "CLEAN"
    with service.memory.connect() as db:
        assert list(db.iterdump()) == before
    (base / manifest["sources"][0]["path"]).write_bytes(b"changed")
    assert not approval_valid(tmp_path, service.memory, record.import_id)[0]
    assert reconcile_imports(tmp_path, service.memory)["recovery_outcome"] == "MANUAL_INTERVENTION_REQUIRED"
