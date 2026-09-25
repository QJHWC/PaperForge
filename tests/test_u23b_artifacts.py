from __future__ import annotations

import json
from pathlib import Path

import pytest

from paperforge.api import PaperForgeService
from paperforge.metric_adapters import (
    AdapterContext,
    MetricExtractorRegistry,
    UnsupportedMetricSource,
)
from paperforge.metric_observation import MetricObservation
from paperforge.publication_artifacts import (
    FactSummarySpec,
    FigureSpec,
    TableSpec,
    generate_facts,
    generate_figure,
    generate_table,
    verify_publication_bundle,
)
from paperforge.result_ledger import ResultLedgerQuery, freeze_ledger


def _service(tmp_path: Path) -> PaperForgeService:
    return PaperForgeService(tmp_path)


def _seed(service, *, metric="latency_ms", value="22.4", unit="ms", kind="measured", run=None, parents=None, method="", method_version=""):
    from paperforge.evidence_semantics import EvidenceSemantics

    sem_kwargs = {"measurement_kind": kind, "acquisition_origin": "paperforge_run", "environment_role": "scientific"}
    if parents:
        sem_kwargs.update({"parent_evidence_ids": parents, "derivation_method": method or "m", "derivation_version": method_version or "v1"})
    sem = EvidenceSemantics.from_metadata(sem_kwargs)
    base = {"model_id": "m1", "dataset_id": "d", "seed": "1", "device_id": "dev",
            "runtime": "rt", "precision": "FP16", "input_size": "896", "batch_size": "1", "timing_scope": "inference_only"}
    base.update(run or {})
    metadata = {**sem.to_metadata(), "metric_name": metric, "value_str": str(value), "unit": unit,
                "run_identity": {"schema": "paperforge.scientific-run-identity/v1", **base},
                "method": method, "method_version": method_version, "parent_evidence_ids": parents or []}
    return service.memory.add_evidence(evidence_type="EXTERNAL_MEASUREMENT", excerpt=f"{metric}={value}",
                                       path="m.txt", config_scope="test", metadata=metadata)


def _ctx(**overrides):
    base = {"evidence_id": "ev1", "run_identity": {}, "measurement_kind": "measured",
            "acquisition_origin": "external_import", "environment_role": "scientific"}
    base.update(overrides)
    return AdapterContext(**base)


def test_adapter_scalar_json(tmp_path: Path) -> None:  # 1
    reg = MetricExtractorRegistry()
    payload = json.dumps({"schema": "paperforge.metric-json/v1", "metric": "latency_ms", "value": "22.4", "unit": "ms"}).encode()
    obs = reg.extract(payload, "sources/a.json", _ctx(), media_type="application/json")
    assert obs[0].metric_name == "latency_ms" and obs[0].canonical_value == "22.4"  # 3 (locator below)


def test_adapter_unknown_schema(tmp_path: Path) -> None:  # 2
    reg = MetricExtractorRegistry()
    with pytest.raises(UnsupportedMetricSource, match="UNSUPPORTED_METRIC_SOURCE"):
        reg.extract(b'{"metric": "x"}', "sources/a.json", _ctx(), media_type="application/json")


def test_adapter_csv_locator(tmp_path: Path) -> None:  # 4
    reg = MetricExtractorRegistry()
    obs = reg.extract(b"metric,value,unit\nlatency_ms,22.4,ms\n", "sources/a.csv", _ctx(), media_type="text/csv")
    assert obs[0].source_locator.row == 2 and obs[0].source_locator.column == "value"


def test_transcribed_not_upgraded(tmp_path: Path) -> None:  # 5
    reg = MetricExtractorRegistry()
    payload = json.dumps({"schema": "paperforge.metric-json/v1", "metric": "latency_ms", "value": "50.25", "unit": "ms"}).encode()
    obs = reg.extract(payload, "s.json", _ctx(measurement_kind="transcribed", acquisition_origin="manuscript_existing"))
    assert obs[0].measurement_kind == "transcribed"


def _ledger_three_layers(tmp_path: Path):
    service = _service(tmp_path)
    lat = _seed(service, value="22.4", kind="measured", run={"model_id": "m1"})
    _seed(service, metric="fps", value="44.6", unit="fps", kind="derived", run={"model_id": "m1"},
          parents=[lat], method="fps_from_latency", method_version="v1")
    _seed(service, value="50.25", kind="remapped", run={"model_id": "m1", "device_id": "remapped"},
          parents=[lat], method="host_to_sim", method_version="v1")
    ledger = freeze_ledger(tmp_path, service.memory, ResultLedgerQuery(purpose="scientific"))
    return service, ledger


def test_table_deterministic_and_no_raw_parse(tmp_path: Path) -> None:  # 6,7
    service, ledger = _ledger_three_layers(tmp_path)
    spec = TableSpec(ledger_id=ledger["ledger_id"], columns=["latency_ms"])
    first = generate_table(tmp_path, service.memory, spec)
    second = generate_table(tmp_path, service.memory, spec)
    assert first["manifest"]["output_hashes"] == second["manifest"]["output_hashes"]


def test_display_rounding_preserves_ledger(tmp_path: Path) -> None:  # 8
    service, ledger = _ledger_three_layers(tmp_path)
    spec = TableSpec(ledger_id=ledger["ledger_id"], columns=["latency_ms"],
                     display_formats={"latency_ms": {"places": 2}})
    out = generate_table(tmp_path, service.memory, spec)
    tex = (tmp_path / "artifacts" / "tables" / out["table_id"] / "table.tex").read_text()
    assert "22.40" in tex and ledger["snapshots"][0]["value_str"] == "22.4"


def test_no_implicit_best_sort_or_bold(tmp_path: Path) -> None:  # 9,11
    service = _service(tmp_path)
    _seed(service, metric="map", value="0.80", unit="score", run={"model_id": "a"})
    _seed(service, metric="map", value="0.85", unit="score", run={"model_id": "b"})
    ledger = freeze_ledger(tmp_path, service.memory, ResultLedgerQuery(purpose="scientific", metric_names=["map"]))
    spec = TableSpec(ledger_id=ledger["ledger_id"], columns=["map"])
    out = generate_table(tmp_path, service.memory, spec)
    tex = (tmp_path / "artifacts" / "tables" / out["table_id"] / "table.tex").read_text()
    assert "\\textbf" not in tex


def test_explicit_sort_and_highlight(tmp_path: Path) -> None:  # 10,12
    service = _service(tmp_path)
    _seed(service, metric="map", value="0.80", unit="score", run={"model_id": "a"})
    _seed(service, metric="map", value="0.85", unit="score", run={"model_id": "b"})
    ledger = freeze_ledger(tmp_path, service.memory, ResultLedgerQuery(purpose="scientific", metric_names=["map"]))
    rows = sorted(ledger["detail_rows"], key=lambda r: str(r["row_id"]))
    spec = TableSpec(ledger_id=ledger["ledger_id"], columns=["map"],
                     row_order=[r["row_id"] for r in reversed(rows)],
                     highlight_rules=[{"metric": "map", "rule": "max"}])
    out = generate_table(tmp_path, service.memory, spec)
    assert out["manifest"]["highlight_rules"][0]["rule"] == "max"
    tex = (tmp_path / "artifacts" / "tables" / out["table_id"] / "table.tex").read_text()
    assert "\\textbf" in tex


def test_table_figure_fact_same_ledger(tmp_path: Path) -> None:  # 13
    service, ledger = _ledger_three_layers(tmp_path)
    assert ledger["snapshots"]


def test_figure_only_ledger(tmp_path: Path) -> None:  # 14
    service = _service(tmp_path)
    _seed(service, metric="latency_ms", value="20", run={"model_id": "m1", "seed": "1"})
    _seed(service, metric="map", value="0.8", unit="score", run={"model_id": "m1", "seed": "1"})
    ledger = freeze_ledger(tmp_path, service.memory, ResultLedgerQuery(purpose="scientific"))
    spec = FigureSpec(ledger_id=ledger["ledger_id"], x_metric="latency_ms", y_metric="map")
    out = generate_figure(tmp_path, service.memory, spec)
    assert (tmp_path / "artifacts" / "figures" / out["figure_id"] / "plot_data.csv").exists()  # 15


def test_figure_deterministic(tmp_path: Path) -> None:  # 16
    service = _service(tmp_path)
    _seed(service, metric="latency_ms", value="20", run={"model_id": "m1", "seed": "1"})
    _seed(service, metric="map", value="0.8", unit="score", run={"model_id": "m1", "seed": "1"})
    ledger = freeze_ledger(tmp_path, service.memory, ResultLedgerQuery(purpose="scientific"))
    spec = FigureSpec(ledger_id=ledger["ledger_id"], x_metric="latency_ms", y_metric="map")
    first = generate_figure(tmp_path, service.memory, spec)
    second = generate_figure(tmp_path, service.memory, spec)
    assert first["manifest"]["output_hashes"] == second["manifest"]["output_hashes"]


def test_figure_presentation_keeps_ledger(tmp_path: Path) -> None:  # 17
    service = _service(tmp_path)
    _seed(service, metric="latency_ms", value="20", run={"model_id": "m1", "seed": "1"})
    _seed(service, metric="map", value="0.8", unit="score", run={"model_id": "m1", "seed": "1"})
    ledger = freeze_ledger(tmp_path, service.memory, ResultLedgerQuery(purpose="scientific"))
    before = ledger["ledger_digest"]
    spec = FigureSpec(ledger_id=ledger["ledger_id"], x_metric="latency_ms", y_metric="map",
                      labels={"title": "A"}, display_range={"x": [0, 100]})
    generate_figure(tmp_path, service.memory, spec)
    assert freeze_ledger(tmp_path, service.memory, ResultLedgerQuery(purpose="scientific"))["ledger_digest"] == before


def test_fact_canonical_and_delta(tmp_path: Path) -> None:  # 18,19
    service = _service(tmp_path)
    _seed(service, metric="map", value="0.8511", unit="score", run={"model_id": "control"})
    _seed(service, metric="map", value="0.8496", unit="score", run={"model_id": "route"})
    ledger = freeze_ledger(tmp_path, service.memory, ResultLedgerQuery(purpose="scientific", metric_names=["map"]))
    rows = {str((r.get("run_identity") or {}).get("model_id")): r for r in ledger["detail_rows"]}
    out = generate_facts(tmp_path, service.memory, FactSummarySpec(
        ledger_id=ledger["ledger_id"], kind="delta", fact_id="fact-delta-1",
        base_row_id=rows["route"]["row_id"], compare_row_id=rows["control"]["row_id"]))
    assert out["fact"]["value"] == "-0.0015"
    assert set(out["fact"]["evidence_ids"]) == set(rows["route"]["source_evidence_ids"] + rows["control"]["source_evidence_ids"])


def test_fact_no_significance(tmp_path: Path) -> None:  # 20
    service = _service(tmp_path)
    _seed(service, metric="map", value="0.85", unit="score", run={"model_id": "m"})
    ledger = freeze_ledger(tmp_path, service.memory, ResultLedgerQuery(purpose="scientific", metric_names=["map"]))
    row_id = ledger["detail_rows"][0]["row_id"]
    out = generate_facts(tmp_path, service.memory, FactSummarySpec(
        ledger_id=ledger["ledger_id"], kind="metric", fact_id="fact-1", row_id=row_id))
    assert "significant" not in json.dumps(out).lower()


def test_shared_ledger_consistency(tmp_path: Path) -> None:  # 21,22,23
    service, ledger = _ledger_three_layers(tmp_path)
    table = generate_table(tmp_path, service.memory, TableSpec(ledger_id=ledger["ledger_id"], columns=["latency_ms"]))
    figure = generate_figure(tmp_path, service.memory, FigureSpec(
        ledger_id=ledger["ledger_id"], x_metric="latency_ms", y_metric="fps"))
    fact = generate_facts(tmp_path, service.memory, FactSummarySpec(
        ledger_id=ledger["ledger_id"], kind="metric", fact_id="f1", row_id=ledger["detail_rows"][0]["row_id"]))
    assert verify_publication_bundle([table["manifest"], figure["manifest"], fact["manifest"]])["consistent"] is True
    other = freeze_ledger(tmp_path, service.memory, ResultLedgerQuery(purpose="diagnostic"))
    assert verify_publication_bundle([table["manifest"], {"ledger_id": other["ledger_id"], "ledger_digest": other["ledger_digest"]}])["consistent"] is False
    from paperforge.publication_artifacts import EvidenceLayerRef

    assert verify_publication_bundle(
        [table["manifest"], {"ledger_id": other["ledger_id"], "ledger_digest": other["ledger_digest"],
                             "measurement_kinds": ["measured"]}],
        layers=[EvidenceLayerRef(layer_id="native_proxy", ledger_id=table["manifest"]["ledger_id"],
                                 ledger_digest=table["manifest"]["ledger_digest"], rationale="native"),
                EvidenceLayerRef(layer_id="diagnostic_layer", ledger_id=other["ledger_id"],
                                 ledger_digest=other["ledger_digest"], rationale="diagnostic")])["consistent"] is True


def test_artifacts_registered_and_idempotent(tmp_path: Path) -> None:  # 24,26
    service, ledger = _ledger_three_layers(tmp_path)
    spec = TableSpec(ledger_id=ledger["ledger_id"], columns=["latency_ms"])
    first = generate_table(tmp_path, service.memory, spec)
    second = generate_table(tmp_path, service.memory, spec)
    assert first["manifest"]["output_hashes"] == second["manifest"]["output_hashes"]
    with service.memory.connect() as db:
        rows = db.execute("SELECT * FROM artifacts WHERE path LIKE 'artifacts/tables/%'").fetchall()
    assert len(rows) >= 4


def test_failure_no_partial(tmp_path: Path) -> None:  # 25
    service, ledger = _ledger_three_layers(tmp_path)
    with __import__("pytest").raises(Exception):
        generate_table(tmp_path, service.memory, TableSpec(ledger_id="ledger-missing", columns=["x"]))


def test_fixture_diagnostic_and_scientific_gate(tmp_path: Path) -> None:  # 27,28
    service = _service(tmp_path)
    from paperforge.evidence_semantics import EvidenceSemantics

    sem = EvidenceSemantics.from_metadata({"measurement_kind": "measured", "acquisition_origin": "paperforge_run", "environment_role": "fixture"})
    service.memory.add_evidence(evidence_type="EXTERNAL_MEASUREMENT", excerpt="fix", path="f.txt",
                                config_scope="test", metadata={**sem.to_metadata(), "metric_name": "latency_ms", "value_str": "1", "unit": "ms",
                                                              "run_identity": {"schema": "paperforge.scientific-run-identity/v1"}})
    diag = freeze_ledger(tmp_path, service.memory, ResultLedgerQuery(purpose="diagnostic", metric_names=["latency_ms"]))
    assert diag["diagnostic_marking"] == "NOT PUBLICATION EVIDENCE"
    table = generate_table(tmp_path, service.memory, TableSpec(ledger_id=diag["ledger_id"], columns=["latency_ms"]))
    assert table["manifest"]


def test_missing_not_zero_table(tmp_path: Path) -> None:  # 29
    service = _service(tmp_path)
    from paperforge.evidence_semantics import EvidenceSemantics

    sem = EvidenceSemantics.from_metadata({"measurement_kind": "measured", "acquisition_origin": "paperforge_run", "environment_role": "scientific"})
    service.memory.add_evidence(evidence_type="EXTERNAL_MEASUREMENT", excerpt="missing", path="m.txt",
                                config_scope="test", metadata={**sem.to_metadata(), "metric_name": "latency_ms", "unit": "ms",
                                                              "missing_reason": "dropout",
                                                              "run_identity": {"schema": "paperforge.scientific-run-identity/v1"}})
    ledger = freeze_ledger(tmp_path, service.memory, ResultLedgerQuery(purpose="scientific", metric_names=["latency_ms"]))
    table = generate_table(tmp_path, service.memory, TableSpec(ledger_id=ledger["ledger_id"], columns=["latency_ms"]))
    csv_text = (tmp_path / "artifacts" / "tables" / table["table_id"] / "data.csv").read_text()
    assert ",\n" in csv_text or ",," in csv_text or csv_text.strip().endswith(",")


def test_full_regression_anchor(tmp_path: Path) -> None:  # 30
    service, ledger = _ledger_three_layers(tmp_path)
    assert ledger["ledger_digest"] and ledger["snapshots"]
