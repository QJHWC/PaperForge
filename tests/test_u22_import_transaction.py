from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from paperforge.api import PaperForgeService
from paperforge.evidence_semantics import EvidenceSemantics
from paperforge.external_import import (
    ConflictError,
    approval_valid,
    approve_import,
    materialize_import,
    stage_import,
    validate_import,
)


def _service(tmp_path: Path) -> PaperForgeService:
    return PaperForgeService(tmp_path)


def _sem(**overrides) -> EvidenceSemantics:
    base = {
        "measurement_kind": "measured",
        "acquisition_origin": "external_import",
        "environment_role": "scientific",
    }
    base.update(overrides)
    return EvidenceSemantics.from_metadata(base)


def _stage(
    tmp_path: Path, name: str = "latency.json", content: str = '{"latency_ms": 22.4}\n'
):
    service = _service(tmp_path)
    src = tmp_path / name
    src.write_text(content, encoding="utf-8")
    record = stage_import(
        tmp_path,
        service.memory,
        semantics=_sem(),
        subject={"model_id": "m1", "dataset_id": "d1", "seed": 0},
        execution={"device": "rpi5", "runtime": "tflite", "timing_scope": "inference_only"},
        source_files=[name],
    )
    return service, record


def _approve_needs_human(tmp_path: Path, service, record) -> None:
    report = validate_import(tmp_path, service.memory, record.import_id)
    ack = [c["check_id"] for c in report["checks"] if c["status"] == "needs_human"]
    approve_import(
        tmp_path,
        service.memory,
        record.import_id,
        approver="local-owner",
        acknowledged_checks=ack,
        rationale="operator attests device rpi5-03, setup photo archived",
    )


def test_happy_path(tmp_path: Path) -> None:
    service, record = _stage(tmp_path)
    _approve_needs_human(tmp_path, service, record)
    out = materialize_import(tmp_path, service.memory, record.import_id)
    assert out["evidence_ids"]
    assert out["idempotent"] is False


def test_validate_recomputes_manifest_hash(tmp_path: Path) -> None:
    service, record = _stage(tmp_path)
    report = validate_import(tmp_path, service.memory, record.import_id)
    assert report["manifest_sha256"] == record.manifest_sha256


def test_source_modified_after_stage_fails_validate(tmp_path: Path) -> None:
    service, record = _stage(tmp_path)
    target = tmp_path / ".paperforge" / "imports" / record.import_id / "sources" / "latency.json"
    target.write_text('{"latency_ms": 99.9}\n', encoding="utf-8")
    report = validate_import(tmp_path, service.memory, record.import_id)
    assert report["overall_status"] == "failed"


def test_source_modified_after_validate_blocks_approve(tmp_path: Path) -> None:
    service, record = _stage(tmp_path)
    validate_import(tmp_path, service.memory, record.import_id)
    target = tmp_path / ".paperforge" / "imports" / record.import_id / "sources" / "latency.json"
    target.write_text('{"latency_ms": 48.7}\n', encoding="utf-8")
    with pytest.raises(Exception, match="changed after validation"):
        _approve_needs_human(tmp_path, service, record)


def test_source_modified_after_approve_blocks_materialize(tmp_path: Path) -> None:
    service, record = _stage(tmp_path)
    _approve_needs_human(tmp_path, service, record)
    target = tmp_path / ".paperforge" / "imports" / record.import_id / "sources" / "latency.json"
    target.write_text('{"latency_ms": 40.1}\n', encoding="utf-8")
    with pytest.raises(Exception, match="approval invalid"):
        materialize_import(tmp_path, service.memory, record.import_id)
    valid, _ = approval_valid(tmp_path, service.memory, record.import_id)
    assert valid is False


def test_validation_failed_cannot_approve(tmp_path: Path) -> None:
    service = _service(tmp_path)
    src = tmp_path / "bad.csv"
    src.write_text("x\n", encoding="utf-8")
    record = stage_import(
        tmp_path,
        service.memory,
        semantics=_sem(),
        subject={"model_id": "", "dataset_id": ""},
        execution={"device": "", "timing_scope": ""},
        source_files=["bad.csv"],
    )
    report = validate_import(tmp_path, service.memory, record.import_id)
    assert report["overall_status"] == "failed"
    with pytest.raises(Exception, match="validation failed"):
        approve_import(tmp_path, service.memory, record.import_id, approver="local-owner")


def test_needs_human_requires_ack_and_rationale(tmp_path: Path) -> None:
    service, record = _stage(tmp_path)
    validate_import(tmp_path, service.memory, record.import_id)
    with pytest.raises(Exception, match="acknowledgement"):
        approve_import(tmp_path, service.memory, record.import_id, approver="local-owner")
    with pytest.raises(Exception, match="rationale"):
        approve_import(
            tmp_path, service.memory, record.import_id, approver="local-owner",
            acknowledged_checks=["device_identity", "conflict"] if False else ["device_identity"],
        )


def test_needs_human_ack_recorded(tmp_path: Path) -> None:
    service, record = _stage(tmp_path)
    report = validate_import(tmp_path, service.memory, record.import_id)
    ack = [c["check_id"] for c in report["checks"] if c["status"] == "needs_human"]
    out = approve_import(
        tmp_path, service.memory, record.import_id, approver="local-owner",
        acknowledged_checks=ack, rationale="attested",
    )
    assert out["binding"]["approver"] == "local-owner"


def test_approval_binds_both_hashes(tmp_path: Path) -> None:
    service, record = _stage(tmp_path)
    report = validate_import(tmp_path, service.memory, record.import_id)
    out = _approve_needs_human(tmp_path, service, record) or approve_import
    # Re-fetch binding via approval_valid path: tamper validation.json
    vpath = tmp_path / ".paperforge" / "imports" / record.import_id / "validation.json"
    data = json.loads(vpath.read_text(encoding="utf-8"))
    data["checks"].append({"check_id": "evil", "status": "passed"})
    vpath.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    valid, reason = approval_valid(tmp_path, service.memory, record.import_id)
    # Either no approval yet or invalid after tamper; approve must refuse.
    assert valid is False


def test_idempotent_materialize(tmp_path: Path) -> None:
    service, record = _stage(tmp_path)
    _approve_needs_human(tmp_path, service, record)
    first = materialize_import(tmp_path, service.memory, record.import_id)
    second = materialize_import(tmp_path, service.memory, record.import_id)
    assert first["evidence_ids"] == second["evidence_ids"]
    assert second["idempotent"] is True


def test_conflict_does_not_overwrite(tmp_path: Path) -> None:
    service, record = _stage(tmp_path, content='{"latency_ms": 40.1}\n')
    _approve_needs_human(tmp_path, service, record)
    materialize_import(tmp_path, service.memory, record.import_id)
    # Same run identity, different value.
    src2 = tmp_path / "latency2.json"
    src2.write_text('{"latency_ms": 48.7}\n', encoding="utf-8")
    record2 = stage_import(
        tmp_path, service.memory, semantics=_sem(),
        subject={"model_id": "m1", "dataset_id": "d1", "seed": 0},
        execution={"device": "rpi5", "runtime": "tflite", "timing_scope": "inference_only"},
        source_files=["latency2.json"],
    )
    _approve_needs_human(tmp_path, service, record2)
    with pytest.raises(ConflictError):
        materialize_import(tmp_path, service.memory, record2.import_id)


def test_unapproved_has_no_evidence(tmp_path: Path) -> None:
    service, record = _stage(tmp_path)
    validate_import(tmp_path, service.memory, record.import_id)
    with memory_evidence_count(service) as count_before:
        with pytest.raises(Exception):
            materialize_import(tmp_path, service.memory, record.import_id)
        assert memory_evidence_count_now(service) == count_before


from contextlib import contextmanager


@contextmanager
def memory_evidence_count(service):
    with service.memory.connect() as db:
        before = db.execute("SELECT COUNT(*) AS c FROM evidence").fetchone()["c"]
    yield before


def memory_evidence_count_now(service) -> int:
    with service.memory.connect() as db:
        return db.execute("SELECT COUNT(*) AS c FROM evidence").fetchone()["c"]


def test_materialized_not_auto_reviewed(tmp_path: Path) -> None:
    service, record = _stage(tmp_path)
    _approve_needs_human(tmp_path, service, record)
    out = materialize_import(tmp_path, service.memory, record.import_id)
    with service.memory.connect() as db:
        row = db.execute("SELECT * FROM evidence WHERE id=?", (out["evidence_ids"][0],)).fetchone()
        meta = json.loads(row["metadata_json"])
    assert meta["scientifically_reviewed"] is False
    assert meta["acquisition_origin"] == "external_import"


def test_fixture_never_materializes(tmp_path: Path) -> None:
    service = _service(tmp_path)
    src = tmp_path / "fix.json"
    src.write_text("{}\n", encoding="utf-8")
    record = stage_import(
        tmp_path, service.memory,
        semantics=_sem(environment_role="fixture"),
        subject={"model_id": "m", "dataset_id": "d"},
        execution={"device": "rpi5", "timing_scope": "inference_only"},
        source_files=["fix.json"],
    )
    validate_import(tmp_path, service.memory, record.import_id)
    # Fixture is never admissible: validation fails, so approval and
    # materialization must both refuse (fail closed, no evidence).
    report = validate_import(tmp_path, service.memory, record.import_id)
    assert report["overall_status"] == "failed"
    with pytest.raises(Exception, match="(validation failed|approval invalid|not admissible)"):
        approve_import(
            tmp_path, service.memory, record.import_id, approver="local-owner",
            acknowledged_checks=[c["check_id"] for c in report["checks"] if c["status"] == "needs_human"],
            rationale="test",
        )
    with pytest.raises(Exception, match="(approval invalid|not admissible|no valid approval)"):
        materialize_import(tmp_path, service.memory, record.import_id)
    assert memory_evidence_count_now(service) == 0


def test_transcribed_cannot_become_measured(tmp_path: Path) -> None:
    service = _service(tmp_path)
    src = tmp_path / "table.txt"
    src.write_text("50.25 ms\n", encoding="utf-8")
    record = stage_import(
        tmp_path, service.memory,
        semantics=_sem(measurement_kind="transcribed", acquisition_origin="manuscript_existing"),
        subject={"model_id": "m", "dataset_id": "d"},
        execution={"device": "rpi5", "timing_scope": "inference_only"},
        source_files=["table.txt"],
    )
    validate_import(tmp_path, service.memory, record.import_id)
    with pytest.raises(Exception):
        # Transcribed is not admissible; materialize refuses.
        report = validate_import(tmp_path, service.memory, record.import_id)
        ack = [c["check_id"] for c in report["checks"] if c["status"] == "needs_human"]
        approve_import(tmp_path, service.memory, record.import_id, approver="local-owner",
                       acknowledged_checks=ack, rationale="t")
        materialize_import(tmp_path, service.memory, record.import_id)


def test_derived_fps_keeps_parent(tmp_path: Path) -> None:
    service, parent = _stage(tmp_path, name="lat.json", content='{"latency_ms": 22.4}\n')
    _approve_needs_human(tmp_path, service, parent)
    parent_out = materialize_import(tmp_path, service.memory, parent.import_id)
    parent_id = parent_out["evidence_ids"][0]
    src = tmp_path / "fps.json"
    src.write_text('{"fps": 44.6}\n', encoding="utf-8")
    sem = EvidenceSemantics.from_metadata({
        "measurement_kind": "derived", "acquisition_origin": "external_import",
        "environment_role": "scientific", "parent_evidence_ids": [parent_id],
        "derivation_method": "fps_from_latency", "derivation_version": "v1",
    })
    record = stage_import(
        tmp_path, service.memory, semantics=sem,
        subject={"model_id": "m1", "dataset_id": "d1"},
        execution={"device": "rpi5", "runtime": "tflite", "timing_scope": "inference_only"},
        source_files=["fps.json"],
    )
    # Different source content avoids run-identity conflict with parent.
    _approve_needs_human(tmp_path, service, record)
    out = materialize_import(tmp_path, service.memory, record.import_id)
    with service.memory.connect() as db:
        row = db.execute("SELECT * FROM evidence WHERE id=?", (out["evidence_ids"][0],)).fetchone()
        meta = json.loads(row["metadata_json"])
    assert parent_id in meta["parent_evidence_ids"]
    assert meta["measurement_kind"] == "derived"


def test_fixed_power_energy_is_not_measured(tmp_path: Path) -> None:
    service, parent = _stage(tmp_path, name="lat2.json", content='{"latency_ms": 22.4}\n')
    # Use distinct subject so parent/child do not conflict on run identity.
    _approve_needs_human(tmp_path, service, parent)
    parent_out = materialize_import(tmp_path, service.memory, parent.import_id)
    parent_id = parent_out["evidence_ids"][0]
    src = tmp_path / "energy.json"
    src.write_text('{"energy_j": 0.139}\n', encoding="utf-8")
    # Correct: derived proxy with parent + method.
    sem = EvidenceSemantics.from_metadata({
        "measurement_kind": "derived", "acquisition_origin": "external_import",
        "environment_role": "scientific", "parent_evidence_ids": [parent_id],
        "derivation_method": "fixed_power_energy", "derivation_version": "6.2W-v1",
    })
    record = stage_import(
        tmp_path, service.memory, semantics=sem,
        subject={"model_id": "m-energy", "dataset_id": "d1"},
        execution={"device": "rpi5", "runtime": "tflite", "timing_scope": "inference_only"},
        source_files=["energy.json"],
    )
    _approve_needs_human(tmp_path, service, record)
    out = materialize_import(tmp_path, service.memory, record.import_id)
    assert out["evidence_ids"]
    # Wrong: claiming measured_energy directly must be refused at materialize.
    with pytest.raises(Exception, match="derived"):
        materialize_import(tmp_path, service.memory, record.import_id, metric="measured_energy")


def test_remapped_requires_lineage(tmp_path: Path) -> None:
    service = _service(tmp_path)
    src = tmp_path / "remap.json"
    src.write_text("{}\n", encoding="utf-8")
    with pytest.raises(Exception, match="parent_evidence_ids|derivation"):
        stage_import(
            tmp_path, service.memory,
            semantics=_sem(measurement_kind="remapped", acquisition_origin="manuscript_existing"),
            subject={"model_id": "m", "dataset_id": "d"},
            execution={"device": "rpi5", "timing_scope": "inference_only"},
            source_files=["remap.json"],
        )


def test_materialize_exception_injection_rolls_back_single_transaction(
    tmp_path: Path, monkeypatch
) -> None:
    """Exception-injection recovery (not a SIGKILL/power-loss proof).

    True crash safety comes from the single SQLite transaction (WAL atomic
    commit); see also test_reopen_after_materialize_is_consistent and
    reconcile_imports. This test only proves Python-exception rollback.
    """
    import paperforge.external_import as ext

    service2 = _service(tmp_path)
    src_a = tmp_path / "fa.json"
    src_b = tmp_path / "fb.json"
    src_a.write_text('{"a": 1}\n', encoding="utf-8")
    src_b.write_text('{"b": 2}\n', encoding="utf-8")
    record2 = stage_import(
        tmp_path, service2.memory, semantics=_sem(),
        subject={"model_id": "m-fail", "dataset_id": "d-fail"},
        execution={"device": "rpi5", "runtime": "tflite", "timing_scope": "inference_only"},
        source_files=["fa.json", "fb.json"],
    )
    _approve_needs_human(tmp_path, service2, record2)
    before = memory_evidence_count_now(service2)
    real_insert = ext._insert_evidence_row
    calls = {"n": 0}

    def flaky(db, **kwargs):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise OSError("simulated exception during materialize")
        return real_insert(db, **kwargs)

    monkeypatch.setattr(ext, "_insert_evidence_row", flaky)
    with pytest.raises(OSError, match="simulated exception"):
        materialize_import(tmp_path, service2.memory, record2.import_id)
    # Single transaction rolled back: no orphan evidence, no MATERIALIZED state.
    assert memory_evidence_count_now(service2) == before
    with service2.memory.connect() as db:
        row = db.execute("SELECT * FROM imports WHERE id=?", (record2.import_id,)).fetchone()
        assert str(row["state"]) != "materialized"
        mat = db.execute(
            "SELECT * FROM import_materializations WHERE import_id=?", (record2.import_id,)
        ).fetchall()
        assert mat == []


def test_old_approval_preserved_but_invalid(tmp_path: Path) -> None:
    service, record = _stage(tmp_path)
    _approve_needs_human(tmp_path, service, record)
    target = tmp_path / ".paperforge" / "imports" / record.import_id / "sources" / "latency.json"
    target.write_text('{"latency_ms": 1.0}\n', encoding="utf-8")
    with service.memory.connect() as db:
        before = db.execute(
            "SELECT COUNT(*) AS c FROM import_approvals WHERE import_id=?", (record.import_id,)
        ).fetchone()["c"]
    assert before >= 1
    with pytest.raises(Exception):
        materialize_import(tmp_path, service.memory, record.import_id)
    with service.memory.connect() as db:
        after = db.execute(
            "SELECT * FROM import_approvals WHERE import_id=?", (record.import_id,)
        ).fetchall()
    assert len(after) >= 1  # old rows preserved
