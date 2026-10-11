import json

import pytest
from test_u23b_artifacts import _seed, _service

from paperforge.publication_artifacts import FigureSpec, PublicationArtifactError, generate_figure
from paperforge.result_ledger import AggregationSpec, ResultLedgerQuery, freeze_ledger


@pytest.mark.parametrize("dimension", [
    "dataset_id", "split_sha256", "checkpoint_sha256", "runtime", "runtime_version",
    "batch_size", "timing_scope",
])
def test_figure_never_pairs_different_run_conditions(tmp_path, dimension):
    service = _service(tmp_path)
    _seed(service, metric="latency_ms", value="20", run={dimension: "a"})
    _seed(service, metric="map", value="0.8", unit="score", run={dimension: "b"})
    ledger = freeze_ledger(tmp_path, service.memory, ResultLedgerQuery(purpose="scientific"))
    out = generate_figure(tmp_path, service.memory, FigureSpec(
        ledger_id=ledger["ledger_id"], x_metric="latency_ms", y_metric="map"))
    data = json.loads((tmp_path / "artifacts/figures" / out["figure_id"] / "plot_data.json").read_text())
    assert data["series"] == {}


def test_figure_rejects_ambiguous_x_observations(tmp_path):
    service = _service(tmp_path)
    _seed(service, metric="latency_ms", value="20")
    _seed(service, metric="map", value="0.8", unit="score")
    ledger = freeze_ledger(tmp_path, service.memory, ResultLedgerQuery(
        purpose="scientific", aggregations=(AggregationSpec(metric="latency_ms"),)))
    with pytest.raises(PublicationArtifactError, match="ambiguous"):
        generate_figure(tmp_path, service.memory, FigureSpec(
            ledger_id=ledger["ledger_id"], x_metric="latency_ms", y_metric="map"))
