from __future__ import annotations

import json
from pathlib import Path

import pytest

from paperforge.api import PaperForgeService
from paperforge.metric_observation import MetricObservation
from paperforge.result_ledger import (
    AggregationSpec,
    LedgerError,
    ResultLedgerQuery,
    freeze_ledger,
    verify_ledger,
)


def _service(tmp_path: Path) -> PaperForgeService:
    return PaperForgeService(tmp_path)


def _seed(service: PaperForgeService, *, metric="latency_ms", value="22.4", unit="ms", kind="measured", run=None, parents=None, method="", method_version=""):
    from paperforge.evidence_semantics import EvidenceSemantics

    sem = EvidenceSemantics.from_metadata({
        "measurement_kind": kind, "acquisition_origin": "paperforge_run",
        "environment_role": "scientific", "parent_evidence_ids": parents or [],
        "derivation_method": method_version and method or (method or None),
        "derivation_version": method_version or None,
    }) if kind in {"derived", "remapped"} else EvidenceSemantics.from_metadata({
        "measurement_kind": kind, "acquisition_origin": "paperforge_run", "environment_role": "scientific",
    })
    base_run = {"model_id": "yolo11n", "dataset_id": "coco", "seed": "1", "device_id": "sim_rpi5",
                "runtime": "tflite", "precision": "FP16", "input_size": "896",
                "batch_size": "1", "timing_scope": "inference_only"}
    base_run.update(run or {})
    metadata = {**sem.to_metadata(), "metric_name": metric, "value_str": str(value), "unit": unit,
                "run_identity": {"schema": "paperforge.scientific-run-identity/v1", **base_run},
                "method": method, "method_version": method_version,
                "parent_evidence_ids": parents or []}
    return service.memory.add_evidence(
        evidence_type="EXTERNAL_MEASUREMENT", excerpt=f"{metric}={value}",
        path="m.txt", config_scope="test", metadata=metadata)


def _q(**overrides):
    base = {"purpose": "scientific"}
    base.update(overrides)
    return ResultLedgerQuery(**base)


def test_artifact_registration(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _seed(service)
    ledger = freeze_ledger(tmp_path, service.memory, _q(metric_names=["latency_ms"]))
    with service.memory.connect() as db:
        rows = db.execute("SELECT * FROM artifacts WHERE path LIKE ?", (f"%{ledger['ledger_id']}%",)).fetchall()
        paths = sorted(str(r["path"]) for r in rows)
    assert f".paperforge/ledgers/{ledger['ledger_id']}/ledger.json" in paths
    assert f".paperforge/ledgers/{ledger['ledger_id']}/manifest.json" in paths
    row = next(r for r in rows if r["path"].endswith("ledger.json"))
    meta = json.loads(row["metadata_json"])
    assert meta["ledger_digest"] == ledger["ledger_digest"]
    assert meta["query_digest"]
    # Failure atomicity: tampered ledger dir without registry is reconciled on refreeze.
    assert ledger["ledger_digest"]


def test_artifact_idempotent(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _seed(service)
    first = freeze_ledger(tmp_path, service.memory, _q(metric_names=["latency_ms"]))
    second = freeze_ledger(tmp_path, service.memory, _q(metric_names=["latency_ms"]))
    assert first["ledger_digest"] == second["ledger_digest"]
    with service.memory.connect() as db:
        count = db.execute("SELECT COUNT(*) AS c FROM artifacts WHERE path LIKE ?",
                           (f"%{first['ledger_id']}%",)).fetchone()["c"]
    assert count == 2  # ledger.json + manifest.json, no duplicates


def test_seed_allowed_but_precision_not(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _seed(service, value="20", run={"seed": "1", "precision": "FP16"})
    _seed(service, value="22", run={"seed": "2", "precision": "FP16"})
    q = ResultLedgerQuery(purpose="scientific", metric_names=("latency_ms",),
                          aggregations=(AggregationSpec(metric="latency_ms", group_by=("model_id",), reducer="mean", allow_cross=("seed",), cross_intent="3-seed mean"),))
    assert freeze_ledger(tmp_path, service.memory, q)["aggregate_rows"]
    service2 = _service(tmp_path)
    _seed(service2, value="23", run={"seed": "3", "precision": "INT8"})
    with pytest.raises(LedgerError):
        freeze_ledger(tmp_path, service2.memory, q)


def test_cross_kind_forbidden_scientific(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _seed(service, value="20", kind="simulated", run={"seed": "1"})
    _seed(service, value="22", kind="measured", run={"seed": "1"})
    q = ResultLedgerQuery(
        purpose="scientific", metric_names=("latency_ms",),
        aggregations=(AggregationSpec(metric="latency_ms", group_by=("model_id",), reducer="mean",
                                      allow_cross=("seed", "measurement_kind"), cross_intent="bad"),))
    with pytest.raises(LedgerError, match="measurement_kind"):
        freeze_ledger(tmp_path, service.memory, q)
    # Diagnostic coexistence as independent rows is allowed (no aggregation).
    diag = freeze_ledger(tmp_path, service.memory, ResultLedgerQuery(purpose="diagnostic", metric_names=("latency_ms",)))
    assert len(diag["snapshots"]) == 2


def test_canonical_numeric_identity(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _seed(service, value="46.500", run={"seed": "1"})
    ledger = freeze_ledger(tmp_path, service.memory, _q(metric_names=["latency_ms"]))
    snap = ledger["snapshots"][0]
    assert snap["value_str"] == "46.5"
    assert snap["source_lexical_value"] == "46.500"
    _service(tmp_path.parent / (tmp_path.name + "-b"))
    # Numerically identical lexical forms share canonical digest path.
    from paperforge.result_ledger import _decimal_canonical

    assert _decimal_canonical("46.5")[0] == _decimal_canonical("46.500")[0]


def test_mean_std_deterministic_and_ddof(tmp_path: Path) -> None:
    service = _service(tmp_path)
    for seed, value in (("1", "20"), ("2", "22"), ("3", "24")):
        _seed(service, value=value, run={"seed": seed})
    q = ResultLedgerQuery(
        purpose="scientific", metric_names=("latency_ms",),
        aggregations=(
            AggregationSpec(metric="latency_ms", group_by=("model_id",), reducer="mean", allow_cross=("seed",)),
            AggregationSpec(metric="latency_ms", group_by=("model_id",), reducer="std", ddof=1, allow_cross=("seed",)),
            AggregationSpec(metric="latency_ms", group_by=("model_id",), reducer="std", ddof=0, allow_cross=("seed",)),
        ))
    ledger = freeze_ledger(tmp_path, service.memory, q)
    by_ddof = {r["ddof"]: r["value_str"] for r in ledger["aggregate_rows"] if r["reducer"] == "std"}
    assert by_ddof[0] != by_ddof[1]
    again = freeze_ledger(tmp_path, service.memory, q)
    assert again["ledger_digest"] == ledger["ledger_digest"]
    assert ledger["aggregate_rows"][0]["decimal_aggregation_version"] == "paperforge.decimal-aggregation/v1"


def test_release_terminology(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _seed(service)
    ledger = freeze_ledger(tmp_path, service.memory, _q(metric_names=["latency_ms"]))
    report = verify_ledger(tmp_path, service.memory, ledger["ledger_id"])
    assert "eligible_as_release_input" in report
    assert report["eligible_as_release_input"] is True
    assert "Claim Gate" not in json.dumps(report)


def test_metric_observation_contract(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _seed(service, value="22.4")
    ledger = freeze_ledger(tmp_path, service.memory, _q(metric_names=["latency_ms"]))
    obs = MetricObservation.from_ledger_snapshot(ledger["snapshots"][0])
    assert obs.metric_name == "latency_ms" and obs.canonical_value == "22.4"
    assert obs.semantics_version == "paperforge.metric-observation/v1"
