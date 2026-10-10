import json

import pytest
from test_u23b_artifacts import _seed, _service

from paperforge.publication_artifacts import PublicationArtifactError, TableSpec, generate_table
from paperforge.result_ledger import ResultLedgerQuery, freeze_ledger


def table(root, service, columns):
    ledger = freeze_ledger(root, service.memory, ResultLedgerQuery())
    out = generate_table(root, service.memory, TableSpec(ledger_id=ledger["ledger_id"], columns=columns))
    return json.loads((root / "artifacts/tables" / out["table_id"] / "table.json").read_bytes())


def test_table_excludes_unrequested_metrics(tmp_path):
    service = _service(tmp_path)
    _seed(service, metric="latency_ms", value="20")
    _seed(service, metric="fps", value="50", unit="fps")
    rows = table(tmp_path, service, ("latency_ms",))["rows"]
    assert len(rows) == 1
    assert rows[0]["metric_values"] == {"latency_ms": "20"}


def test_table_pivots_matching_runs_and_keeps_cell_provenance(tmp_path):
    service = _service(tmp_path)
    latency = _seed(service, metric="latency_ms", value="20")
    fps = _seed(service, metric="fps", value="50", unit="fps")
    rows = table(tmp_path, service, ("latency_ms", "fps"))["rows"]
    assert len(rows) == 1
    assert rows[0]["metric_values"] == {"latency_ms": "20", "fps": "50"}
    assert set(rows[0]["source_evidence_ids"]) == {latency, fps}
    assert rows[0]["metric_units"] == {"latency_ms": "ms", "fps": "fps"}
    assert rows[0]["metric_source_evidence_ids"] == {"latency_ms": [latency], "fps": [fps]}


@pytest.mark.parametrize("change", [{"dataset_id": "other"}, {"seed": "2"}, {"device_id": "other"}])
def test_table_never_pivots_different_runs(tmp_path, change):
    service = _service(tmp_path)
    _seed(service, metric="latency_ms", value="20")
    _seed(service, metric="fps", value="50", unit="fps", run=change)
    rows = table(tmp_path, service, ("latency_ms", "fps"))["rows"]
    assert len(rows) == 2
    assert all(sum(v is not None for v in row["metric_values"].values()) == 1 for row in rows)


def test_table_rejects_ambiguous_duplicate_metric(tmp_path):
    service = _service(tmp_path)
    _seed(service, value="20")
    _seed(service, value="0.020", unit="s")
    with pytest.raises(PublicationArtifactError, match="ambiguous table metric"):
        table(tmp_path, service, ("latency_ms",))
