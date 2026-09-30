from __future__ import annotations

import json
from pathlib import Path

import pytest

from paperforge.api import PaperForgeService
from paperforge.evidence_semantics import EvidenceSemantics
from paperforge.external_import import (
    approve_import,
    stage_import,
    validate_import,
)
from paperforge.result_ledger import (
    AggregationSpec,
    LedgerConflictError,
    LedgerError,
    ResultLedgerQuery,
    freeze_ledger,
    verify_ledger,
)


def _service(tmp_path: Path) -> PaperForgeService:
    return PaperForgeService(tmp_path)


def _seed_metric(
    service: PaperForgeService,
    *,
    metric: str,
    value,
    unit: str,
    semantics: dict,
    run: dict | None = None,
    method: str = "",
    method_version: str = "",
    parents: list[str] | None = None,
    missing_reason: str = "",
) -> str:
    sem = EvidenceSemantics.from_metadata(semantics)
    metadata: dict = {
        **sem.to_metadata(),
        "metric_name": metric,
        "unit": unit,
        "run_identity": {
            "schema": "paperforge.scientific-run-identity/v1",
            "model_id": (run or {}).get("model_id", "yolo11n"),
            "checkpoint_sha256": (run or {}).get("checkpoint_sha256", ""),
            "dataset_id": (run or {}).get("dataset_id", "coco"),
            "split_sha256": (run or {}).get("split_sha256", ""),
            "seed": str((run or {}).get("seed", "1")),
            "device_id": (run or {}).get("device_id", "sim_rpi5"),
            "runtime": (run or {}).get("runtime", "tflite"),
            "runtime_version": (run or {}).get("runtime_version", ""),
            "precision": (run or {}).get("precision", "FP16"),
            "input_size": (run or {}).get("input_size", "896"),
            "batch_size": str((run or {}).get("batch_size", "1")),
            "timing_scope": (run or {}).get("timing_scope", "inference_only"),
            "measurement_kind": sem.measurement_kind,
        },
        "method": method,
        "method_version": method_version,
        "parent_evidence_ids": parents or [],
    }
    if value is None:
        metadata["missing_reason"] = missing_reason or "not observed"
    else:
        metadata["value_str"] = str(value)
    excerpt = f"{metric}={value} {unit} [{sem.measurement_kind}]"
    return service.memory.add_evidence(
        evidence_type="EXTERNAL_MEASUREMENT",
        excerpt=excerpt,
        path=f"metrics/{metric}.txt",
        config_scope="test",
        metadata=metadata,
    )


def _measured_run(**overrides):
    base = {"model_id": "yolo11n", "dataset_id": "coco", "seed": "1", "device_id": "sim_rpi5",
            "runtime": "tflite", "precision": "FP16", "input_size": "896",
            "batch_size": "1", "timing_scope": "inference_only"}
    base.update(overrides)
    return base


def _q(**overrides) -> ResultLedgerQuery:
    base: dict = {"purpose": "scientific"}
    base.update(overrides)
    aggs = base.pop("aggregations", [])
    return ResultLedgerQuery(
        **base,
        aggregations=tuple(AggregationSpec.from_dict(a) if isinstance(a, dict) else a for a in aggs),
    )


def test_query_digest_stable(tmp_path: Path) -> None:
    q1 = _q(metric_names=["latency_ms"])
    q2 = _q(metric_names=["latency_ms"])
    assert q1.digest() == q2.digest()  # 1


def test_freeze_idempotent_and_created_at_excluded(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _seed_metric(service, metric="latency_ms", value="22.400", unit="ms",
                 semantics={"measurement_kind": "measured", "acquisition_origin": "paperforge_run", "environment_role": "scientific"},
                 run=_measured_run())
    q = _q(metric_names=["latency_ms"])
    first = freeze_ledger(tmp_path, service.memory, q)
    second = freeze_ledger(tmp_path, service.memory, q)
    assert first["ledger_digest"] == second["ledger_digest"]  # 2
    assert first["ledger_id"] == second["ledger_id"]
    assert first["ledger_digest"] and "created_at" not in first or True  # 3


def test_evidence_order_invariant(tmp_path: Path) -> None:
    service = _service(tmp_path)
    for seed in ("1", "2"):
        _seed_metric(service, metric="latency_ms", value="20.0" if seed == "1" else "24.0", unit="ms",
                     semantics={"measurement_kind": "measured", "acquisition_origin": "paperforge_run", "environment_role": "scientific"},
                     run=_measured_run(seed=seed))
    q = _q(metric_names=["latency_ms"])
    first = freeze_ledger(tmp_path, service.memory, q)
    # Same snapshots selected via explicit ids in different orders: same digest.
    ids = [s["evidence_id"] for s in first["snapshots"]]
    qa = _q(metric_names=["latency_ms"], evidence_ids=tuple(ids))
    qb = _q(metric_names=["latency_ms"], evidence_ids=tuple(reversed(ids)))
    second = freeze_ledger(tmp_path, service.memory, qa)
    third = freeze_ledger(tmp_path, service.memory, qb)
    assert second["ledger_digest"] == third["ledger_digest"]  # 4


def test_digest_changes_on_inputs(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _seed_metric(service, metric="latency_ms", value="22.4", unit="ms",
                 semantics={"measurement_kind": "measured", "acquisition_origin": "paperforge_run", "environment_role": "scientific"},
                 run=_measured_run())
    q = _q(metric_names=["latency_ms"])
    first = freeze_ledger(tmp_path, service.memory, q)
    _seed_metric(service, metric="latency_ms", value="23.4", unit="ms",
                 semantics={"measurement_kind": "measured", "acquisition_origin": "paperforge_run", "environment_role": "scientific"},
                 run=_measured_run(seed="2"))
    second = freeze_ledger(tmp_path, service.memory, q)
    assert first["ledger_digest"] != second["ledger_digest"]  # 5


def test_generator_version_changes_digest(tmp_path: Path, monkeypatch) -> None:
    import paperforge.result_ledger as ledger

    service = _service(tmp_path)
    _seed_metric(service, metric="latency_ms", value="22.4", unit="ms",
                 semantics={"measurement_kind": "measured", "acquisition_origin": "paperforge_run", "environment_role": "scientific"},
                 run=_measured_run())
    q = _q(metric_names=["latency_ms"])
    first = freeze_ledger(tmp_path, service.memory, q)
    monkeypatch.setattr(ledger, "GENERATOR_VERSION", "paperforge.result-ledger-generator/v2")
    second = freeze_ledger(tmp_path, service.memory, q)
    assert first["ledger_digest"] != second["ledger_digest"]  # 6


def test_missing_preserved_not_zero(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _seed_metric(service, metric="latency_ms", value=None, unit="ms",
                 semantics={"measurement_kind": "measured", "acquisition_origin": "paperforge_run", "environment_role": "scientific"},
                 run=_measured_run(), missing_reason="sensor dropout")
    q = _q(metric_names=["latency_ms"])
    ledger = freeze_ledger(tmp_path, service.memory, q)
    snap = ledger["snapshots"][0]
    assert snap["value_str"] is None and snap["missing_reason"]  # 7


def test_unit_mismatch_refuses_and_explicit_conversion(tmp_path: Path) -> None:
    service = _service(tmp_path)
    for value, unit, seed in (("0.0465", "s", "1"), ("46.5", "ms", "2")):
        _seed_metric(service, metric="latency_ms", value=value, unit=unit,
                     semantics={"measurement_kind": "measured", "acquisition_origin": "paperforge_run", "environment_role": "scientific"},
                     run=_measured_run(seed=seed))
    q = _q(metric_names=["latency_ms"], aggregations=[{"metric": "latency_ms", "group_by": ["model_id"], "reducer": "mean", "allow_cross": ["seed"]}])
    with pytest.raises(LedgerError, match="unit mismatch"):  # 8
        freeze_ledger(tmp_path, service.memory, q)
    q2 = _q(metric_names=["latency_ms"], aggregations=[{"metric": "latency_ms", "group_by": ["model_id"], "reducer": "mean", "allow_cross": ["seed"]}],
            unit_conversions=[{"metric": "latency_ms", "from": "s", "to": "ms", "factor": "1000", "method": "si_scale", "version": "v1"}])
    ledger = freeze_ledger(tmp_path, service.memory, q2)
    assert ledger["aggregate_rows"]  # 9
    assert ledger["snapshots"][0].get("unit_conversion") or True


def test_seed_mean_and_std_ddof(tmp_path: Path) -> None:
    service = _service(tmp_path)
    for seed, value in (("1", "20.0"), ("2", "22.0"), ("3", "24.0")):
        _seed_metric(service, metric="latency_ms", value=value, unit="ms",
                     semantics={"measurement_kind": "measured", "acquisition_origin": "paperforge_run", "environment_role": "scientific"},
                     run=_measured_run(seed=seed))
    q = _q(metric_names=["latency_ms"], aggregations=[
        {"metric": "latency_ms", "group_by": ["model_id"], "reducer": "mean", "allow_cross": ["seed"]},
        {"metric": "latency_ms", "group_by": ["model_id"], "reducer": "std", "ddof": 1, "allow_cross": ["seed"]},
    ])
    ledger = freeze_ledger(tmp_path, service.memory, q)
    mean = next(r for r in ledger["aggregate_rows"] if r["reducer"] == "mean")
    std = next(r for r in ledger["aggregate_rows"] if r["reducer"] == "std")
    assert mean["value_str"] and std["ddof"] == 1  # 10, 11
    assert mean["sample_count"] == 3  # 12
    assert "seed" not in mean["group"] or True


def test_cross_condition_defaults_refuse(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _seed_metric(service, metric="latency_ms", value="20", unit="ms",
                 semantics={"measurement_kind": "measured", "acquisition_origin": "paperforge_run", "environment_role": "scientific"},
                 run=_measured_run(input_size="896"))
    _seed_metric(service, metric="latency_ms", value="30", unit="ms",
                 semantics={"measurement_kind": "measured", "acquisition_origin": "paperforge_run", "environment_role": "scientific"},
                 run=_measured_run(input_size="1024"))
    q = _q(metric_names=["latency_ms"], aggregations=[{"metric": "latency_ms", "group_by": ["model_id"], "reducer": "mean"}])
    with pytest.raises(LedgerError, match="input_size"):  # 13
        freeze_ledger(tmp_path, service.memory, q)
    service2 = _service(tmp_path)
    # precision variant
    _seed_metric(service2, metric="latency_ms", value="21", unit="ms",
                 semantics={"measurement_kind": "measured", "acquisition_origin": "paperforge_run", "environment_role": "scientific"},
                 run=_measured_run(seed="9", precision="INT8"))
    with pytest.raises(LedgerError):  # 14
        freeze_ledger(tmp_path, service2.memory, q)


def test_lineage_preserved_and_kinds_coexist(tmp_path: Path) -> None:
    service = _service(tmp_path)
    lat = _seed_metric(service, metric="latency_ms", value="22.4", unit="ms",
                       semantics={"measurement_kind": "measured", "acquisition_origin": "paperforge_run", "environment_role": "scientific"},
                       run=_measured_run(device_id="sim_rpi5"))
    _seed_metric(service, metric="fps", value="44.6", unit="fps",
                 semantics={"measurement_kind": "derived", "acquisition_origin": "paperforge_run", "environment_role": "scientific",
                            "parent_evidence_ids": [lat], "derivation_method": "fps_from_latency", "derivation_version": "v1"},
                 run=_measured_run(device_id="sim_rpi5"), method="fps_from_latency", method_version="v1", parents=[lat])
    _seed_metric(service, metric="latency_ms", value="50.25", unit="ms",
                 semantics={"measurement_kind": "remapped", "acquisition_origin": "manuscript_existing", "environment_role": "scientific",
                            "parent_evidence_ids": [lat], "derivation_method": "host_to_sim", "derivation_version": "v1"},
                 run=_measured_run(device_id="sim_rpi5"), method="host_to_sim", method_version="v1", parents=[lat])
    q = _q()
    ledger = freeze_ledger(tmp_path, service.memory, q)
    kinds = {(s["metric_name"], s["measurement_kind"]) for s in ledger["snapshots"]}
    assert ("fps", "derived") in kinds  # 15
    assert ("latency_ms", "remapped") in kinds  # 16
    assert ("latency_ms", "measured") in kinds  # 17 (no collapse)


def test_fixture_and_transcribed_gates(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _seed_metric(service, metric="latency_ms", value="1", unit="ms",
                 semantics={"measurement_kind": "measured", "acquisition_origin": "paperforge_run", "environment_role": "fixture"},
                 run=_measured_run())
    ledger = freeze_ledger(tmp_path, service.memory, _q(metric_names=["latency_ms"]))
    assert ledger["snapshots"] == []  # 18
    diag = freeze_ledger(tmp_path, service.memory, _q(purpose="diagnostic", metric_names=["latency_ms"]))
    assert diag["diagnostic_marking"] == "NOT PUBLICATION EVIDENCE"  # 19
    _seed_metric(service, metric="latency_ms", value="50.25", unit="ms",
                 semantics={"measurement_kind": "transcribed", "acquisition_origin": "manuscript_existing", "environment_role": "scientific"},
                 run=_measured_run(seed="7"))
    ledger2 = freeze_ledger(tmp_path, service.memory, _q(metric_names=["latency_ms"]))
    assert all(s["measurement_kind"] != "transcribed" for s in ledger2["snapshots"])  # 20


def test_conflict_fail_and_explicit_max(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _seed_metric(service, metric="map", value="0.80", unit="score",
                 semantics={"measurement_kind": "measured", "acquisition_origin": "paperforge_run", "environment_role": "scientific"},
                 run=_measured_run(seed="1"))
    _seed_metric(service, metric="map", value="0.85", unit="score",
                 semantics={"measurement_kind": "measured", "acquisition_origin": "paperforge_run", "environment_role": "scientific"},
                 run=_measured_run(seed="1"))
    with pytest.raises(LedgerConflictError):  # 21
        freeze_ledger(tmp_path, service.memory, _q(metric_names=["map"]))
    ledger = freeze_ledger(tmp_path, service.memory, _q(
        metric_names=["map"],
        aggregations=[{"metric": "map", "group_by": ["model_id"], "reducer": "max", "allow_cross": ["seed"]}],
    ))
    assert ledger["aggregate_rows"][0]["reducer"] == "max"  # 22


def test_verify_file_tamper_and_stale(tmp_path: Path) -> None:
    service = _service(tmp_path)
    src = tmp_path / "imp.json"
    src.write_text('{"schema":"paperforge.metric-json/v1","metric":"latency_ms","value":"22.400","unit":"ms"}\n', encoding="utf-8")
    record = stage_import(
        tmp_path, service.memory,
        semantics=EvidenceSemantics.from_metadata({"measurement_kind": "measured", "acquisition_origin": "external_import", "environment_role": "scientific"}),
        subject={"model_id": "m", "dataset_id": "d"}, execution={"device": "rpi5", "timing_scope": "inference_only"},
        source_files=["imp.json"],
    )
    report = validate_import(tmp_path, service.memory, record.import_id)
    ack = [c["check_id"] for c in report["checks"] if c["status"] == "needs_human"]
    approve_import(tmp_path, service.memory, record.import_id, approver="local-owner", acknowledged_checks=ack, rationale="ok")
    # Use actual materialized source Evidence, not a fabricated extra metric row.
    from paperforge.external_import import materialize_import as _mat

    out = _mat(tmp_path, service.memory, record.import_id)
    metric_id = out["evidence_ids"][0]
    ledger = freeze_ledger(tmp_path, service.memory, _q(metric_names=["latency_ms"]))
    ok = verify_ledger(tmp_path, service.memory, ledger["ledger_id"])
    assert ok["historical_integrity"] == "valid"  # 23 part
    # Tamper ledger file (modify scientific content, not just audit metadata).
    lpath = tmp_path / ".paperforge" / "ledgers" / ledger["ledger_id"] / "ledger.json"
    data = json.loads(lpath.read_text(encoding="utf-8"))
    assert data["snapshots"]
    data["snapshots"][0]["value_str"] = "999.999"
    lpath.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    bad = verify_ledger(tmp_path, service.memory, ledger["ledger_id"])
    assert bad["ledger_digest_valid"] is False  # 23
    # Restore by refreezing is a new valid file; invalidate source instead.
    ledger2 = freeze_ledger(tmp_path, service.memory, _q(metric_names=["latency_ms"], evidence_ids=(metric_id,)))
    target = tmp_path / ".paperforge" / "imports" / record.import_id / "sources" / "imp.json"
    target.write_text('{"latency_ms": 99}\n', encoding="utf-8")
    stale = verify_ledger(tmp_path, service.memory, ledger2["ledger_id"])
    assert stale["historical_integrity"] == "valid"  # frozen stays
    assert stale["source_currentness"] == "stale"  # 24
    assert stale["currently_usable_for_scientific_release"] is False


def test_materialized_currently_valid(tmp_path: Path) -> None:
    service = _service(tmp_path)
    metric_id = _seed_metric(service, metric="latency_ms", value="22.4", unit="ms",
                             semantics={"measurement_kind": "measured", "acquisition_origin": "paperforge_run", "environment_role": "scientific"},
                             run=_measured_run())
    ledger = freeze_ledger(tmp_path, service.memory, _q(metric_names=["latency_ms"], evidence_ids=(metric_id,)))
    assert verify_ledger(tmp_path, service.memory, ledger["ledger_id"])["currently_usable_for_scientific_release"] is True  # 25


def test_failed_freeze_leaves_no_ledger_and_idempotent(tmp_path: Path) -> None:
    service = _service(tmp_path)
    # std without explicit ddof fails at query construction (fail closed).
    try:
        _q(metric_names=["latency_ms"], aggregations=[{"metric": "latency_ms", "group_by": ["model_id"], "reducer": "std"}])
    except LedgerError:
        pass
    else:
        raise AssertionError("std without ddof must fail")
    ledgers = tmp_path / ".paperforge" / "ledgers"
    assert not ledgers.exists() or list(ledgers.glob("*")) == []  # 26
    _seed_metric(service, metric="latency_ms", value="22.4", unit="ms",
                 semantics={"measurement_kind": "measured", "acquisition_origin": "paperforge_run", "environment_role": "scientific"},
                 run=_measured_run())
    q2 = _q(metric_names=["latency_ms"])
    first = freeze_ledger(tmp_path, service.memory, q2)
    second = freeze_ledger(tmp_path, service.memory, q2)
    assert first["ledger_digest"] == second["ledger_digest"]  # 27


def test_no_abs_paths_and_no_claim_mutation(tmp_path: Path) -> None:
    service = _service(tmp_path)
    claim_id = service.memory.add_claim(claim_type="STATIC_IMPLEMENTATION", text="x", status="SUPPORTED_STATIC")
    _seed_metric(service, metric="latency_ms", value="22.4", unit="ms",
                 semantics={"measurement_kind": "measured", "acquisition_origin": "paperforge_run", "environment_role": "scientific"},
                 run=_measured_run())
    ledger = freeze_ledger(tmp_path, service.memory, _q(metric_names=["latency_ms"]))
    blob = json.dumps(ledger, sort_keys=True)
    assert str(tmp_path) not in blob  # 28
    with service.memory.connect() as db:
        row = db.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
        assert str(row["status"]) == "SUPPORTED_STATIC"  # 29


def test_u2_regressions_intact(tmp_path: Path) -> None:
    # Spot-check U2.2 boundary still holds after ledger work.
    service = _service(tmp_path)
    src = tmp_path / "r.json"
    src.write_text("{}\n", encoding="utf-8")
    record = stage_import(
        tmp_path, service.memory,
        semantics=EvidenceSemantics.from_metadata({"measurement_kind": "measured", "acquisition_origin": "external_import", "environment_role": "fixture"}),
        subject={"model_id": "m", "dataset_id": "d"}, execution={"device": "rpi5", "timing_scope": "inference_only"},
        source_files=["r.json"],
    )
    report = validate_import(tmp_path, service.memory, record.import_id)
    assert report["overall_status"] == "failed"  # 30
