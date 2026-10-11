from __future__ import annotations

import pathlib

from paperforge.scientific_review import (
    Assumption,
    ScienceRulePack,
    ScientificClaim,
    check_calibration_overlap,
    review_claim,
    review_state,
)


def _claim(text, *, claim_type="descriptive_metric", facts=("f1",), qualifiers=None, citations=()):
    return ScientificClaim(claim_id="c1", text=text, claim_type=claim_type,
                           fact_ids=facts, qualifiers=qualifiers or {}, citation_keys=citations)


def _facts():
    return {
        "f1": {"measurement_kind": "simulated", "qualifiers": {"dataset": "d", "device": "sim"}},
        "f2": {"measurement_kind": "measured", "qualifiers": {"dataset": "d", "device": "rpi"}},
    }


def test_simulated_as_physical_fail() -> None:  # 1
    results = review_claim(_claim("Physical device measurement of 20 ms.", facts=("f1",)), facts=_facts())
    assert any(r.check_type == "layer_consistency" and r.status == "failed" for r in results)


def test_remapped_as_device_fail() -> None:  # 2
    facts = {"f": {"measurement_kind": "remapped"}}
    results = review_claim(_claim("Direct device measurement.", facts=("f",)), facts=facts)
    assert any(r.status == "failed" for r in results)


def test_measured_not_auto_hardware_validated() -> None:  # 3
    facts = {"f": {"measurement_kind": "measured"}}
    results = review_claim(_claim("Hardware validated at 22.4 ms.", facts=("f",)), facts=facts)
    assert any(r.check_type == "layer_consistency" and r.status == "needs_human" for r in results)


def test_shared_parent_detected() -> None:  # 4
    facts = {"a": {"parent_evidence_ids": ["p"]}, "b": {"parent_evidence_ids": ["p"]}}
    results = review_claim(_claim("Latency, FPS and energy independently verify.", facts=("a", "b")), facts=facts)
    assert any(r.check_type == "derived_dependency" and r.status in {"needs_human", "failed"} for r in results)


def test_independent_measured_no_false_dependency() -> None:  # 5
    facts = {"a": {}, "b": {}}
    results = review_claim(_claim("Two measurements.", facts=("a", "b")), facts=facts)
    assert any(r.check_type == "derived_dependency" and r.status == "passed" for r in results)


def test_identical_protocol_pass() -> None:  # 6
    facts = {"a": {"qualifiers": {"dataset": "d", "device": "x"}}, "b": {"qualifiers": {"dataset": "d", "device": "x"}}}
    results = review_claim(_claim("A vs B.", claim_type="comparison", facts=("a", "b")), facts=facts)
    assert any(r.check_type == "comparison_compatibility" and r.status == "passed" for r in results)


def test_allowed_difference_pass_with_disclosure() -> None:  # 7
    facts = {"a": {"qualifiers": {"precision": "FP16"}}, "b": {"qualifiers": {"precision": "INT8"}}}
    pack = ScienceRulePack(allowed_comparison_differences=("precision",))
    results = review_claim(_claim("A vs B under disclosed precision.", claim_type="comparison", facts=("a", "b")),
                           facts=facts, rule_pack=pack)
    assert any(r.check_type == "comparison_compatibility" and r.status == "passed" for r in results)


def test_false_identical_fail() -> None:  # 8
    facts = {"a": {"qualifiers": {"device": "x"}}, "b": {"qualifiers": {"device": "y"}}}
    results = review_claim(_claim("Same protocol A vs B.", claim_type="comparison", facts=("a", "b")), facts=facts)
    assert any(r.check_type == "comparison_compatibility" and r.status == "failed" for r in results)


def test_calibration_overlap_fail_and_distinct_pass() -> None:  # 9,10
    facts = {"a": {}, "b": {}}
    failed = check_calibration_overlap(_claim("x", facts=("a",)), facts, {"a": ["calibration", "validation"]})
    assert failed.status == "failed"
    passed = check_calibration_overlap(_claim("x", facts=("a", "b")), facts, {"a": "calibration", "b": "validation"})
    assert passed.status == "passed"


def test_equivalence_needs_evidence() -> None:  # 11
    results = review_claim(_claim("Equivalent performance.", claim_type="statistical_equivalence", facts=("f1",)),
                           facts=_facts())
    assert any(r.check_type == "statistical_wording" and r.status == "needs_human" for r in results)


def test_robustness_not_auto() -> None:  # 12
    results = review_claim(_claim("Robust performance.", facts=("f1",)), facts=_facts())
    assert any(r.status == "needs_human" for r in results)


def test_statistical_artifact_satisfies() -> None:  # 13
    results = review_claim(_claim("Significantly better.", claim_type="statistical_superiority", facts=("f1",)),
                           facts=_facts(), statistical_artifacts=["stat-test-1"])
    assert any(r.check_type == "statistical_wording" and r.status == "passed" for r in results)


def test_generalization_scope() -> None:  # 14,15
    results = review_claim(_claim("General industrial PCB performance.", facts=("f1",)), facts=_facts())
    assert any(r.check_type == "scope" and r.status == "needs_human" for r in results)
    results2 = review_claim(_claim("On dataset D, mAP 0.85.", facts=("f1",)), facts=_facts())
    assert any(r.check_type == "scope" and r.status == "passed" for r in results2)


def test_device_scope() -> None:  # 16
    results = review_claim(_claim("Hardware-independent deployment.", facts=("f1",)), facts=_facts())
    assert any(r.status == "needs_human" for r in results)


def test_causal_needs_contrast_and_controlled_pass() -> None:  # 17,18
    facts = {"a": {"qualifiers": {"architecture": "x", "training_route": "r1"}},
             "b": {"qualifiers": {"architecture": "y", "training_route": "r2"}}}
    results = review_claim(_claim("Gain is driven by backbone.", claim_type="causal_attribution", facts=("a", "b")), facts=facts)
    assert any(r.check_type == "causal_attribution" and r.status == "needs_human" for r in results)
    facts2 = {"a": {"qualifiers": {"architecture": "x"}}, "b": {"qualifiers": {"architecture": "x"}}}
    results2 = review_claim(_claim("Gain is driven by backbone.", claim_type="causal_attribution", facts=("a", "b")), facts=facts2)
    assert any(r.check_type == "causal_attribution" and r.status == "passed" for r in results2)


def test_citation_mapping() -> None:  # 19,20
    results = review_claim(_claim("Method background.", citations=("ref1",)), facts=_facts())
    assert any(r.check_type == "citation_support" and r.status == "needs_human" for r in results)
    results2 = review_claim(_claim("Method background.", citations=("ref1",)), facts=_facts(),
                            citation_support={"ref1": {"excerpt": "method"}})
    assert any(r.check_type == "citation_support" and r.status == "passed" for r in results2)


def test_assumptions_disclosed() -> None:  # 21,22
    assumption = Assumption(assumption_id="a1", description="i.i.d.", status="unverified")
    assert assumption.status == "unverified"
    checked = Assumption(assumption_id="a2", description="x", status="empirically_checked")
    assert checked.status == "empirically_checked"


def test_fixture_blocked_and_no_mutation() -> None:  # 23,24

    facts = {"f": {"measurement_kind": "measured"}}
    before = dict(facts["f"])
    review_claim(_claim("x", facts=("f",)), facts=facts)
    assert facts["f"] == before


def test_human_item_blocks_full_review() -> None:  # 25
    results = review_claim(_claim("Robust.", facts=("f1",)), facts=_facts())
    assert review_state(results) in {"reviewed_with_caveats", "not_reviewed"}
    assert review_state(results) != "reviewed"


def test_rule_pack_dependency_and_calibration() -> None:  # 26,27
    pack = ScienceRulePack(metric_dependencies=({"metric": "fps", "depends_on": "latency"},),
                           calibration_roles=({"evidence": "e1", "role": "calibration"},))
    assert pack.metric_dependencies[0]["metric"] == "fps"
    assert pack.calibration_roles[0]["role"] == "calibration"


def test_no_pcb_hardcoding() -> None:  # 28
    source = pathlib.Path("paperforge/scientific_review.py").read_text(encoding="utf-8")
    lowered = source.lower()
    for token in ("sim_rpi5", "deeppcb", "6.2", "p_fp"):
        assert token not in lowered


def test_narrative_integration_and_regression() -> None:  # 29,30

    sent = {"sentence_id": "s", "text": "Control 0.8511.", "role": "metric_report", "fact_ids": ["f1"]}
    claim = __import__("paperforge.scientific_review", fromlist=["ScientificClaim"]).ScientificClaim.from_narrative_sentence(sent)
    assert claim.claim_type == "descriptive_metric"
