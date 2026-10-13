from dataclasses import replace

import pytest

from paperforge.scientific_review import (
    ScientificClaim,
    StatisticalEvidence,
    check_statistical_binding,
    coverage_gate,
    review_claim,
)


def claim():
    return ScientificClaim("c", "A and B are equivalent.", "statistical_equivalence",
                           subjects=("A", "B"), fact_ids=("f",), qualifiers={
                               "metric": "map", "dataset": "D", "split": "test",
                               "evidence_ids": ["e1", "e2"], "margin": "0.01"})


def support():
    return StatisticalEvidence("equivalence", "map", subjects=("A", "B"),
                               dataset="D", split="test", evidence_ids=("e1", "e2"),
                               method="TOST", method_version="v1", result="pass", margin="0.01",
                               source_artifact_hash="a" * 64)


def test_arbitrary_artifact_name_cannot_clear_statistical_review():
    item = claim()
    results = review_claim(item, facts={"f": {}}, statistical_artifacts=["unrelated-string"])
    assert coverage_gate([item], results)["c"]["verdict"] != "reviewed"


@pytest.mark.parametrize("changes", [
    {"metric": "fps"}, {"dataset": "other"}, {"split": "train"},
    {"subjects": ("A", "C")}, {"evidence_ids": ("e1", "unrelated")},
    {"margin": "0.02"}, {"margin": "NaN"}, {"margin": "-0.01"},
    {"source_artifact_hash": ""}, {"method": ""}, {"result": ""},
])
def test_statistical_support_requires_complete_binding(changes):
    item = replace(claim(), fact_ids=("e1", "e2"))
    assert check_statistical_binding(item, [replace(support(), **changes)]).status != "passed"


@pytest.mark.parametrize("entry", ["string", {}, {"subjects": "A", "evidence_ids": "e1"}])
def test_malformed_support_fails_closed(entry):
    assert check_statistical_binding(claim(), [entry]).status != "passed"


def test_correctly_bound_statistical_metadata_passes_binding():
    assert check_statistical_binding(claim(), [support()]).status == "passed"


def test_coverage_requires_strict_statistical_binding():
    item = claim()
    results = [r for r in review_claim(item) if r.check_type != "statistical_binding"]
    assert coverage_gate([item], results)["c"]["verdict"] == "not_reviewed"


@pytest.mark.parametrize("result", ["pass", "fail", "equivalence rejected; p=0.9"])
def test_matching_metadata_does_not_approve_statistical_interpretation(result):
    item = claim()
    results = review_claim(item, facts={"f": {}}, statistical_evidence=[replace(support(), result=result)])
    assert coverage_gate([item], results)["c"]["verdict"] == "needs_human"


@pytest.mark.parametrize("text,kind,expected", [("A vs B.", "statistical_equivalence", "needs_human"),
    ("Statistically indistinguishable.", "descriptive_metric", "needs_human"),
    ("A is noninferior to B.", "comparison", "failed")])
def test_statistical_gate_cannot_be_bypassed_by_claim_classification(text, kind, expected):
    item = replace(claim(), text=text, claim_type=kind)
    results = review_claim(item, facts={"f": {}}, statistical_evidence=[support()])
    assert coverage_gate([item], results)["c"]["verdict"] == expected
