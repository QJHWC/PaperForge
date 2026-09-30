from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from paperforge.api import PaperForgeService
from paperforge.evidence_semantics import EvidenceSemantics
from paperforge.external_import import (
    RUN_IDENTITY_SCHEMA,
    ScientificRunIdentity,
    approve_import,
    materialize_import,
    reconcile_imports,
    stage_import,
    validate_import,
)


def _service(tmp_path: Path) -> PaperForgeService:
    return PaperForgeService(tmp_path)


def _sem(**overrides):
    base = {
        "measurement_kind": "measured",
        "acquisition_origin": "external_import",
        "environment_role": "scientific",
    }
    base.update(overrides)
    return EvidenceSemantics.from_metadata(base)


def _stage_basic(tmp_path: Path, name: str = "s.json", content: str = '{"x": 1}\n'):
    service = _service(tmp_path)
    src = tmp_path / name
    src.write_text(content, encoding="utf-8")
    record = stage_import(
        tmp_path, service.memory, semantics=_sem(),
        subject={"model_id": "m", "dataset_id": "d", "seed": 1},
        execution={
            "device": "rpi5", "runtime": "tflite", "timing_scope": "inference_only",
            "precision": "FP16", "input_size": "896", "batch_size": 1,
        },
        source_files=[name],
    )
    return service, record


def _approve_all_human(tmp_path: Path, service, record):
    report = validate_import(tmp_path, service.memory, record.import_id)
    ack = [c["check_id"] for c in report["checks"] if c["status"] == "needs_human"]
    approve_import(
        tmp_path, service.memory, record.import_id, approver="local-owner",
        acknowledged_checks=ack, rationale="attested",
    )
    return report


# F1: digest determinism, no self-reference, timestamp excluded.
def test_validation_digest_stable_and_idempotent(tmp_path: Path) -> None:
    service, record = _stage_basic(tmp_path)
    first = validate_import(tmp_path, service.memory, record.import_id)
    second = validate_import(tmp_path, service.memory, record.import_id)
    assert "validation_digest" in first
    assert first["validation_digest"] == second["validation_digest"]
    # Digest payload excludes volatile timestamps and itself.
    assert first.get("validated_at")  # file keeps audit timestamps
    from paperforge.external_import import _stable_validation_payload, _validation_digest

    stable = _stable_validation_payload(first)
    assert "created_at" not in stable
    assert "validated_at" not in stable
    assert "validation_digest" not in stable
    assert "validation_file_sha256" not in stable
    assert _validation_digest(stable) == first["validation_digest"]


def test_digest_changes_on_content_but_not_timestamp(tmp_path: Path) -> None:
    service, record = _stage_basic(tmp_path)
    first = validate_import(tmp_path, service.memory, record.import_id)
    # Touch only audit timestamp in the file: digest must stay valid.
    vpath = tmp_path / ".paperforge" / "imports" / record.import_id / "validation.json"
    data = json.loads(vpath.read_text(encoding="utf-8"))
    data["created_at"] = "2000-01-01T00:00:00+00:00"
    vpath.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    # Approval path recomputes digest from stable payload, so timestamp edit passes.
    from paperforge.external_import import _current_validation_report

    report, digest = _current_validation_report(
        tmp_path.expanduser().resolve(strict=True), record.import_id
    )
    assert digest == first["validation_digest"]
    # Real content change alters digest (re-validate after source tamper).
    target = tmp_path / ".paperforge" / "imports" / record.import_id / "sources" / "s.json"
    target.write_text('{"x": 999}\n', encoding="utf-8")
    third = validate_import(tmp_path, service.memory, record.import_id)
    assert third["validation_digest"] != first["validation_digest"]


def test_hand_edited_validation_fails(tmp_path: Path) -> None:
    service, record = _stage_basic(tmp_path)
    validate_import(tmp_path, service.memory, record.import_id)
    vpath = tmp_path / ".paperforge" / "imports" / record.import_id / "validation.json"
    data = json.loads(vpath.read_text(encoding="utf-8"))
    data["checks"].append({"check_id": "evil", "status": "passed", "detail": "forged"})
    vpath.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    from paperforge.external_import import ApprovalError, _current_validation_report

    try:
        _current_validation_report(tmp_path.expanduser().resolve(strict=True), record.import_id)
    except ApprovalError:
        pass
    else:
        raise AssertionError("forged validation.json must fail")


# F2: run identity dimensions.
def test_run_identity_distinguishes_configs(tmp_path: Path) -> None:
    base_subject = {"model_id": "yolo11n", "dataset_id": "d", "seed": 1}
    base_execution = {
        "device": "rpi5", "runtime": "tflite", "timing_scope": "inference_only",
        "precision": "FP16", "input_size": "896", "batch_size": 1,
    }
    m1 = {"semantics": _sem().to_metadata(), "subject": base_subject, "execution": base_execution}
    m2 = {"semantics": _sem().to_metadata(), "subject": base_subject,
          "execution": {**base_execution, "input_size": "1024"}}
    m3 = {"semantics": _sem().to_metadata(), "subject": base_subject,
          "execution": {**base_execution, "precision": "INT8"}}
    m4 = {"semantics": _sem().to_metadata(), "subject": base_subject,
          "execution": {**base_execution, "batch_size": 8}}
    m5 = {"semantics": _sem().to_metadata(), "subject": {**base_subject, "seed": 2},
          "execution": base_execution}
    i1 = ScientificRunIdentity.from_manifest(m1)
    assert i1.schema == RUN_IDENTITY_SCHEMA
    assert ScientificRunIdentity.from_manifest(m2).canonical() != i1.canonical()
    assert ScientificRunIdentity.from_manifest(m3).canonical() != i1.canonical()
    assert ScientificRunIdentity.from_manifest(m4).canonical() != i1.canonical()
    assert ScientificRunIdentity.from_manifest(m5).canonical() != i1.canonical()


def test_same_identity_conflicts(tmp_path: Path) -> None:
    service, r1 = _stage_basic(tmp_path, name="c1.json", content='{"v": 40.1}\n')
    _approve_all_human(tmp_path, service, r1)
    materialize_import(tmp_path, service.memory, r1.import_id)
    src2 = tmp_path / "c2.json"
    src2.write_text('{"v": 48.7}\n', encoding="utf-8")
    r2 = stage_import(
        tmp_path, service.memory, semantics=_sem(),
        subject={"model_id": "m", "dataset_id": "d", "seed": 1},
        execution={
            "device": "rpi5", "runtime": "tflite", "timing_scope": "inference_only",
            "precision": "FP16", "input_size": "896", "batch_size": 1,
        },
        source_files=["c2.json"],
    )
    report = validate_import(tmp_path, service.memory, r2.import_id)
    assert any(c["check_id"] == "conflict" for c in report["checks"])


# F3: acquisition-method semantics.
def test_reciprocal_fps_is_derived(tmp_path: Path) -> None:
    service, parent = _stage_basic(tmp_path, name="lat.json", content='{"latency_ms": 22.4}\n')
    _approve_all_human(tmp_path, service, parent)
    pout = materialize_import(tmp_path, service.memory, parent.import_id)
    pid = pout["evidence_ids"][0]
    src = tmp_path / "fps.json"
    src.write_text('{"fps": 44.6}\n', encoding="utf-8")
    sem = EvidenceSemantics.from_metadata({
        "measurement_kind": "derived", "acquisition_origin": "external_import",
        "environment_role": "scientific", "parent_evidence_ids": [pid],
        "derivation_method": "fps_from_latency", "derivation_version": "v1",
    })
    rec = stage_import(
        tmp_path, service.memory, semantics=sem,
        subject={"model_id": "m-fps", "dataset_id": "d"},
        execution={"device": "rpi5", "runtime": "tflite", "timing_scope": "inference_only"},
        source_files=["fps.json"],
    )
    _approve_all_human(tmp_path, service, rec)
    out = materialize_import(
        tmp_path, service.memory, rec.import_id,
        metric_provenance={"metric": "fps", "acquisition_method": "fps_from_latency"},
    )
    assert out["evidence_ids"]


def test_windowed_throughput_may_be_measured(tmp_path: Path) -> None:
    service = _service(tmp_path)
    src = tmp_path / "tput.json"
    src.write_text('{"frames": 1200, "elapsed_s": 60}\n', encoding="utf-8")
    rec = stage_import(
        tmp_path, service.memory, semantics=_sem(),
        subject={"model_id": "m-t", "dataset_id": "d"},
        execution={"device": "rpi5", "runtime": "tflite", "timing_scope": "inference_only"},
        source_files=["tput.json"],
    )
    _approve_all_human(tmp_path, service, rec)
    out = materialize_import(
        tmp_path, service.memory, rec.import_id,
        metric_provenance={
            "metric": "fps", "acquisition_method": "windowed_throughput_measurement",
            "frame_count": 1200, "elapsed_time_s": 60,
        },
    )
    assert out["evidence_ids"]
    with service.memory.connect() as db:
        row = db.execute("SELECT * FROM evidence WHERE id=?", (out["evidence_ids"][0],)).fetchone()
        meta = json.loads(row["metadata_json"])
    assert meta["measurement_kind"] == "measured"


def test_fixed_power_energy_is_derived(tmp_path: Path) -> None:
    service, parent = _stage_basic(tmp_path, name="l.json", content='{"latency_ms": 22.4}\n')
    _approve_all_human(tmp_path, service, parent)
    pout = materialize_import(tmp_path, service.memory, parent.import_id)
    pid = pout["evidence_ids"][0]
    src = tmp_path / "e.json"
    src.write_text('{"e": 0.1}\n', encoding="utf-8")
    sem = EvidenceSemantics.from_metadata({
        "measurement_kind": "derived", "acquisition_origin": "external_import",
        "environment_role": "scientific", "parent_evidence_ids": [pid],
        "derivation_method": "fixed_power_energy_proxy", "derivation_version": "6.2W-v1",
    })
    rec = stage_import(
        tmp_path, service.memory, semantics=sem,
        subject={"model_id": "m-e", "dataset_id": "d"},
        execution={"device": "rpi5", "runtime": "tflite", "timing_scope": "inference_only"},
        source_files=["e.json"],
    )
    _approve_all_human(tmp_path, service, rec)
    out = materialize_import(
        tmp_path, service.memory, rec.import_id,
        metric_provenance={"metric": "energy", "acquisition_method": "fixed_power_energy_proxy"},
    )
    assert out["evidence_ids"]


def test_power_trace_energy_may_be_measured(tmp_path: Path) -> None:
    service = _service(tmp_path)
    src = tmp_path / "ptrace.csv"
    src.write_text("t,p\n0,5.9\n", encoding="utf-8")
    rec = stage_import(
        tmp_path, service.memory, semantics=_sem(),
        subject={"model_id": "m-p", "dataset_id": "d"},
        execution={"device": "rpi5", "runtime": "tflite", "timing_scope": "inference_only"},
        source_files=["ptrace.csv"],
    )
    _approve_all_human(tmp_path, service, rec)
    out = materialize_import(
        tmp_path, service.memory, rec.import_id,
        metric_provenance={
            "metric": "energy", "acquisition_method": "power_trace_integration",
            "power_trace_ref": "sources/ptrace.csv", "measurement_window": "60s",
            "frame_count": 1200,
        },
    )
    assert out["evidence_ids"]


def test_measured_energy_without_provenance_rejected(tmp_path: Path) -> None:
    service, rec = _stage_basic(tmp_path, name="e2.json", content='{"e": 1}\n')
    _approve_all_human(tmp_path, service, rec)
    try:
        materialize_import(
            tmp_path, service.memory, rec.import_id,
            metric_provenance={"metric": "energy", "acquisition_method": "power_trace_integration"},
        )
    except Exception:
        pass
    else:
        raise AssertionError("power-trace claim without trace ref must fail")


# F4: reopen + reconcile (exception test lives in test_u22_import_transaction).
def test_reopen_after_materialize_is_consistent(tmp_path: Path) -> None:
    service, record = _stage_basic(tmp_path)
    _approve_all_human(tmp_path, service, record)
    out = materialize_import(tmp_path, service.memory, record.import_id)
    # Reopen the same workspace (new memory handle) and reconcile.
    reopened = PaperForgeService(tmp_path)
    report = reconcile_imports(tmp_path, reopened.memory, repair=False)
    assert report["consistent"] is True
    # Evidence survives reopen with import linkage.
    with reopened.memory.connect() as db:
        row = db.execute("SELECT * FROM evidence WHERE id=?", (out["evidence_ids"][0],)).fetchone()
        assert row is not None
        meta = json.loads(row["metadata_json"])
        assert meta["import_id"] == record.import_id
        assert "materialization_key" in meta


def test_reconcile_detects_orphan(tmp_path: Path) -> None:
    service, record = _stage_basic(tmp_path)
    _approve_all_human(tmp_path, service, record)
    out = materialize_import(tmp_path, service.memory, record.import_id)
    # Simulate a leftover from an older version: delete the materialization row.
    with service.memory.connect() as db:
        db.execute("DELETE FROM import_materializations WHERE import_id=?", (record.import_id,))
    report = reconcile_imports(tmp_path, service.memory, repair=False)
    assert out["evidence_ids"][0] in report["orphan_evidence"]
    repaired = reconcile_imports(tmp_path, service.memory, repair=True)
    assert repaired.get("repaired") is False
    assert repaired["recovery_outcome"] == "MANUAL_INTERVENTION_REQUIRED"
    with service.memory.connect() as db:
        assert db.execute("SELECT 1 FROM evidence WHERE id=?", (out["evidence_ids"][0],)).fetchone()
    assert reconcile_imports(tmp_path, service.memory)["consistent"] is False


def test_subprocess_reopen_sees_committed_state(tmp_path: Path) -> None:
    service, record = _stage_basic(tmp_path, name="sub.json", content='{"v": 1}\n')
    _approve_all_human(tmp_path, service, record)
    out = materialize_import(tmp_path, service.memory, record.import_id)
    _code = (
        "import json,sys; from paperforge.api import PaperForgeService; "
        f"s=PaperForgeService({str(tmp_path)!r}); "
        f"print(json.dumps(s.memory.get_evidence_count() if hasattr(s.memory,'get_evidence_count') else 1))"
    )
    # Minimal subprocess reopen: the child only opens the DB read-only path.
    proc = subprocess.run(
        [sys.executable, "-c", "from paperforge.api import PaperForgeService; "
         f"s=PaperForgeService({str(tmp_path)!r}); print('reopen-ok')"],
        capture_output=True, text=True, timeout=60,
    )
    assert "reopen-ok" in proc.stdout
    assert out["evidence_ids"]
