from __future__ import annotations

import json
from pathlib import Path

import pytest

from paperforge.api import PaperForgeService
from paperforge.metric_adapters import (
    AdapterContext,
    MetricExtractorRegistry,
    SemanticsMismatchError,
)
from paperforge.publication_artifacts import (
    EvidenceLayerRef,
    TableSpec,
    generate_table,
    verify_publication_bundle,
)
from paperforge.result_ledger import ResultLedgerQuery, freeze_ledger


def _service(tmp_path: Path) -> PaperForgeService:
    return PaperForgeService(tmp_path)


def _ctx(**overrides):
    base = {"evidence_id": "ev1", "run_identity": {"model_id": "m1", "input_size": "896", "precision": "FP16", "seed": "1"},
            "measurement_kind": "simulated", "acquisition_origin": "paperforge_run", "environment_role": "scientific"}
    base.update(overrides)
    return AdapterContext(**base)


def test_simulated_source_claiming_measured_rejected() -> None:
    reg = MetricExtractorRegistry()
    payload = json.dumps({"schema": "paperforge.metric-json/v1", "metric": "latency_ms",
                          "value": "20", "unit": "ms", "measurement_kind": "measured"}).encode()
    with pytest.raises(SemanticsMismatchError, match="SEMANTICS_MISMATCH"):
        reg.extract(payload, "s.json", _ctx(), media_type="application/json")


def test_fixture_claiming_scientific_rejected() -> None:
    reg = MetricExtractorRegistry()
    payload = json.dumps({"schema": "paperforge.metric-json/v1", "metric": "latency_ms",
                          "value": "20", "unit": "ms", "environment_role": "scientific"}).encode()
    with pytest.raises(SemanticsMismatchError, match="SEMANTICS_MISMATCH"):
        reg.extract(payload, "s.json", _ctx(measurement_kind="measured", environment_role="fixture"))


def test_run_identity_mismatch_rejected() -> None:
    reg = MetricExtractorRegistry()
    payload = json.dumps({"schema": "paperforge.metric-json/v1", "metric": "latency_ms",
                          "value": "20", "unit": "ms", "input_size": "1024"}).encode()
    with pytest.raises(SemanticsMismatchError, match="SEMANTICS_MISMATCH"):
        reg.extract(payload, "s.json", _ctx(), media_type="application/json")


def test_normal_extraction_keeps_evidence_semantics() -> None:
    reg = MetricExtractorRegistry()
    payload = json.dumps({"schema": "paperforge.metric-json/v1", "metric": "latency_ms",
                          "value": "20", "unit": "ms"}).encode()
    obs = reg.extract(payload, "s.json", _ctx(), media_type="application/json")
    assert obs[0].measurement_kind == "simulated" and obs[0].environment_role == "scientific"


def _seed(service, *, value="20", kind="measured", run=None):
    from paperforge.evidence_semantics import EvidenceSemantics

    sem = EvidenceSemantics.from_metadata({"measurement_kind": kind, "acquisition_origin": "paperforge_run", "environment_role": "scientific"})
    base = {"model_id": "m1", "dataset_id": "d", "seed": "1", "device_id": "dev", "runtime": "rt",
            "precision": "FP16", "input_size": "896", "batch_size": "1", "timing_scope": "inference_only"}
    base.update(run or {})
    return service.memory.add_evidence(
        evidence_type="EXTERNAL_MEASUREMENT", excerpt=f"m={value}", path="m.txt", config_scope="test",
        metadata={**sem.to_metadata(), "metric_name": "latency_ms", "value_str": str(value), "unit": "ms",
                  "run_identity": {"schema": "paperforge.scientific-run-identity/v1", **base}})


def test_typed_layers(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _seed(service, value="20")
    ledger = freeze_ledger(tmp_path, service.memory, ResultLedgerQuery(purpose="scientific", metric_names=["latency_ms"]))
    table = generate_table(tmp_path, service.memory, TableSpec(ledger_id=ledger["ledger_id"], columns=["latency_ms"]))
    other = freeze_ledger(tmp_path, service.memory, ResultLedgerQuery(purpose="diagnostic", metric_names=["latency_ms"]))
    # 1 same ledger pass
    assert verify_publication_bundle([table["manifest"], table["manifest"]])["consistent"] is True
    # 2 different no layer fail
    assert verify_publication_bundle([table["manifest"], {"ledger_id": other["ledger_id"], "ledger_digest": other["ledger_digest"]}])["consistent"] is False
    # 3 valid typed layers pass
    layers = [EvidenceLayerRef(layer_id="a", ledger_id=table["manifest"]["ledger_id"], ledger_digest=table["manifest"]["ledger_digest"], rationale="a"),
              EvidenceLayerRef(layer_id="b", ledger_id=other["ledger_id"], ledger_digest=other["ledger_digest"], rationale="b")]
    assert verify_publication_bundle(
        [table["manifest"], {"ledger_id": other["ledger_id"], "ledger_digest": other["ledger_digest"], "measurement_kinds": []}],
        layers=layers)["consistent"] is True
    # 4 digest mismatch fail
    bad = [EvidenceLayerRef(layer_id="a", ledger_id=table["manifest"]["ledger_id"], ledger_digest="0" * 64, rationale="x"),
           EvidenceLayerRef(layer_id="b", ledger_id=other["ledger_id"], ledger_digest=other["ledger_digest"], rationale="y")]
    assert verify_publication_bundle(
        [table["manifest"], {"ledger_id": other["ledger_id"], "ledger_digest": other["ledger_digest"]}],
        layers=bad)["consistent"] is False
    # 5 ambiguous (two layers same ledger) fail
    dup = [EvidenceLayerRef(layer_id="a", ledger_id=table["manifest"]["ledger_id"], ledger_digest=table["manifest"]["ledger_digest"]),
           EvidenceLayerRef(layer_id="b", ledger_id=table["manifest"]["ledger_id"], ledger_digest=table["manifest"]["ledger_digest"])]
    assert verify_publication_bundle([table["manifest"]], layers=dup)["consistent"] is False
    # 6 kind incompatible fail
    kind_layers = [EvidenceLayerRef(layer_id="a", ledger_id=table["manifest"]["ledger_id"], ledger_digest=table["manifest"]["ledger_digest"],
                                    measurement_kinds=["measured"], rationale="x")]
    manifest = dict(table["manifest"])
    manifest["measurement_kinds"] = ["simulated"]
    assert verify_publication_bundle([manifest], layers=kind_layers)["consistent"] is False
    # arbitrary mapping bypass refused
    with pytest.raises(Exception, match="typed layer"):
        verify_publication_bundle([table["manifest"]], declared_layers={"whatever": True})


def test_readiness_not_publication_ready(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _seed(service)
    ledger = freeze_ledger(tmp_path, service.memory, ResultLedgerQuery(purpose="scientific", metric_names=["latency_ms"]))
    table = generate_table(tmp_path, service.memory, TableSpec(ledger_id=ledger["ledger_id"], columns=["latency_ms"]))
    assert table["manifest"]["artifact_stage"] == "publication_candidate"
    assert table["manifest"]["publication_ready"] is False
    bundle = verify_publication_bundle([table["manifest"], table["manifest"]])
    assert bundle["publication_readiness"] == "candidate_not_ready"
    assert bundle["consistent"] is True
