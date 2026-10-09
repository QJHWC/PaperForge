import pytest
from test_u23b_artifacts import _ledger_three_layers, _seed, _service

from paperforge.grounded_narrative import (
    GroundedNarrativeSpec,
    NarrativeGroundingError,
    NarrativeSentence,
    validate_narrative,
)
from paperforge.publication_artifacts import (
    FactSummarySpec,
    PublicationArtifactError,
    generate_facts,
)
from paperforge.result_ledger import ResultLedgerQuery, freeze_ledger
from paperforge.scientific_review import ScientificClaim, check_comparison_compatibility


@pytest.mark.parametrize("kind", ["metric", "aggregation", "provenance"])
def test_generated_facts_preserve_layer_and_identity(tmp_path, kind):
    service, ledger = _ledger_three_layers(tmp_path)
    row = next(r for r in ledger["detail_rows"] if r["measurement_kind"] == "remapped")
    fact = generate_facts(tmp_path, service.memory, FactSummarySpec(
        ledger_id=ledger["ledger_id"], row_id=row["row_id"], kind=kind))["fact"]
    assert fact["measurement_kind"] == "remapped"
    assert fact["run_identity"] == row["run_identity"]
    with pytest.raises(NarrativeGroundingError, match="layer wording"):
        validate_narrative(GroundedNarrativeSpec(allowed_fact_ids=(fact["fact_id"],)),
                           [[NarrativeSentence("claim", "Physical device measurement.",
                                               fact_ids=(fact["fact_id"],))]], facts=[fact])


def test_generated_fact_comparison_detects_different_dataset(tmp_path):
    service = _service(tmp_path)
    _seed(service, value="20", run={"dataset_id": "a"})
    _seed(service, value="22", run={"dataset_id": "b"})
    ledger = freeze_ledger(tmp_path, service.memory, ResultLedgerQuery())
    facts = [generate_facts(tmp_path, service.memory, FactSummarySpec(
        ledger_id=ledger["ledger_id"], row_id=row["row_id"]))["fact"] for row in ledger["detail_rows"]]
    result = check_comparison_compatibility(ScientificClaim(
        "claim", "Comparison under the same protocol.", "comparison",
        fact_ids=tuple(f["fact_id"] for f in facts)), {f["fact_id"]: f for f in facts})
    assert result.status == "failed"


def test_fact_qualifiers_cannot_override_source_identity(tmp_path):
    service, ledger = _ledger_three_layers(tmp_path)
    row = ledger["detail_rows"][0]
    with pytest.raises(PublicationArtifactError, match="qualifier"):
        generate_facts(tmp_path, service.memory, FactSummarySpec(
            ledger_id=ledger["ledger_id"], row_id=row["row_id"], qualifiers={"dataset": "different"}))


def test_delta_refuses_different_datasets(tmp_path):
    service = _service(tmp_path)
    _seed(service, value="20", run={"dataset_id": "a"})
    _seed(service, value="22", run={"dataset_id": "b"})
    ledger = freeze_ledger(tmp_path, service.memory, ResultLedgerQuery())
    a, b = ledger["detail_rows"]
    with pytest.raises(PublicationArtifactError, match="identity"):
        generate_facts(tmp_path, service.memory, FactSummarySpec(
            ledger_id=ledger["ledger_id"], kind="delta", base_row_id=a["row_id"], compare_row_id=b["row_id"]))
