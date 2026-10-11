import pytest
from test_u23b_artifacts import _seed, _service

from paperforge.result_ledger import AggregationSpec, LedgerError, ResultLedgerQuery, freeze_ledger


def test_aggregation_cannot_silently_pool_models(tmp_path):
    service = _service(tmp_path)
    for model, value in (("A", "1"), ("B", "3")):
        _seed(service, value=value, run={"model_id": model})
    query = ResultLedgerQuery(aggregations=(AggregationSpec(metric="latency_ms"),))
    with pytest.raises(LedgerError, match="differing model_id"):
        freeze_ledger(tmp_path, service.memory, query)


def test_model_grouping_keeps_results_separate(tmp_path):
    service = _service(tmp_path)
    for model, value in (("A", "1"), ("B", "3")):
        _seed(service, value=value, run={"model_id": model})
    query = ResultLedgerQuery(aggregations=(AggregationSpec(metric="latency_ms", group_by=("model_id",)),))
    ledger = freeze_ledger(tmp_path, service.memory, query)
    assert {r["group"]["model_id"]: r["value_str"] for r in ledger["aggregate_rows"]} == {"A": "1", "B": "3"}
