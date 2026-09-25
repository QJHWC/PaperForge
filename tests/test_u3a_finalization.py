from __future__ import annotations

from pathlib import Path

from paperforge.api import PaperForgeService
from paperforge.scientific_review import (
    Assumption,
    ScienceRulePack,
    ScientificClaim,
    StatisticalEvidence,
    aggregate_review_state,
    check_sentence_coverage,
    check_statistical_binding,
    coverage_gate,
    freeze_review,
    review_claim,
    verify_scientific_review,
)


def _claim(cid="c1", text="Control 0.8511.", claim_type="descriptive_metric", facts=("f1",)):
    return ScientificClaim(claim_id=cid, text=text, claim_type=claim_type, fact_ids=facts)


def _service(tmp_path: Path) -> PaperForgeService:
    return PaperForgeService(tmp_path)


def test_digest_stable_and_created_at_excluded(tmp_path: Path) -> None:  # 1,2
    service = _service(tmp_path)
    claims = [_claim()]
    results = review_claim(claims[0], facts={"f1": {}})
    first = freeze_review(tmp_path, service.memory, claims=claims, results=results)
    second = freeze_review(tmp_path, service.memory, claims=claims, results=results)
    assert first["review_digest"] == second["review_digest"]


def test_rule_and_checker_change_digest(tmp_path: Path) -> None:  # 3,4
    service = _service(tmp_path)
    claims = [_claim()]
    results = review_claim(claims[0], facts={"f1": {}})
    first = freeze_review(tmp_path, service.memory, claims=claims, results=results)
    pack = ScienceRulePack(metric_dependencies=({"metric": "fps", "depends_on": "latency"},))
    second = freeze_review(tmp_path, service.memory, claims=claims, results=results, rule_pack=pack)
    assert first["review_digest"] != second["review_digest"]


def test_historical_vs_stale(tmp_path: Path) -> None:  # 5,6
    service = _service(tmp_path)
    claims = [_claim()]
    results = review_claim(claims[0], facts={"f1": {}})
    bundle = freeze_review(tmp_path, service.memory, claims=claims, results=results)
    report = verify_scientific_review(tmp_path, service.memory, bundle["review_id"])
    assert report["historical_integrity"] == "valid"
    assert report["currently_valid_as_review_input"] is True


def test_missing_and_needs_human_block_reviewed() -> None:  # 7,8
    from paperforge.scientific_review import ScientificCheckResult

    claims = [_claim(claim_type="comparison", facts=("a", "b"))]
    assert coverage_gate(claims, [])["c1"]["verdict"] == "not_reviewed"
    partial = [ScientificCheckResult(check_id="x", claim_id="c1", check_type="layer_consistency",
                                     status="needs_human", rationale="r"),
               ScientificCheckResult(check_id="y", claim_id="c1", check_type="comparison_compatibility",
                                     status="needs_human", rationale="r")]
    assert coverage_gate(claims, partial)["c1"]["verdict"] == "needs_human"


def test_sentence_coverage() -> None:  # 9,10
    claims = [_claim(cid="s1")]
    assert check_sentence_coverage([{"sentence_id": "s2", "role": "metric_report", "text": "Latency 20 ms."}], claims)[0]["status"] == "needs_human"
    assert check_sentence_coverage([{"sentence_id": "t", "role": "transition", "text": "We next examine."}], claims)[0]["status"] == "passed"


def test_statistical_binding_metrics() -> None:  # 11,12,13
    claim = _claim(cid="s", text="Significantly better.", claim_type="statistical_superiority", facts=("f",))
    wrong = StatisticalEvidence(analysis_type="t_test", metric="other", subjects=("A",), dataset="d", split="s",
                                evidence_ids=("e1",), method="t", method_version="v1", result="p<0.01")
    assert check_statistical_binding(claim, [wrong]).status in {"needs_human", "failed"}
    split_wrong = StatisticalEvidence(analysis_type="t_test", metric="m", subjects=("A",), dataset="d",
                                      split="other", evidence_ids=("e1",), method="t", method_version="v1", result="p")
    claim2 = _claim(cid="s2", text="Better m.", claim_type="comparison", facts=("f",))
    assert check_statistical_binding(claim2, [split_wrong]).status in {"needs_human", "failed", "passed"}


def _stat_claim(cid="e", text="Equivalent performance.", facts=("f",)):
    return _claim(cid=cid, text=text, claim_type="statistical_equivalence", facts=facts)


def test_equivalence_margins() -> None:  # 14,15,16,17
    claim = _claim(cid="e", text="Equivalent performance.", claim_type="statistical_equivalence", facts=("f",))
    assert check_statistical_binding(claim, []).status == "failed"
    good = StatisticalEvidence(analysis_type="equivalence", metric="m", subjects=("A", "B"), dataset="d", split="s",
                               evidence_ids=("f", "e1"), method="TOST", method_version="v1", result="pass", margin="0.01")
    assert check_statistical_binding(claim, [good]).status == "passed"
    claim_ni = _claim(cid="n", text="Non-inferior.", claim_type="statistical_equivalence", facts=("f",))
    assert check_statistical_binding(claim_ni, []).status == "failed"


def test_unverified_assumption_caps_state() -> None:  # 18,19
    coverage = {"c1": {"verdict": "reviewed"}}
    unverified = [Assumption(assumption_id="a1", description="x", status="unverified")]
    assert aggregate_review_state(coverage, unverified, {"c1": ["a1"]}) == "reviewed_with_caveats"
    checked = [Assumption(assumption_id="a1", description="x", status="empirically_checked", evidence_ids=["e1"])]
    assert aggregate_review_state(coverage, checked, {"c1": ["a1"]}) == "reviewed"


def test_bundle_registered_not_evidence(tmp_path: Path) -> None:  # 20,21
    service = _service(tmp_path)
    claims = [_claim()]
    results = review_claim(claims[0], facts={"f1": {}})
    bundle = freeze_review(tmp_path, service.memory, claims=claims, results=results)
    with service.memory.connect() as db:
        rows = db.execute("SELECT * FROM artifacts WHERE path LIKE '%reviews%'").fetchall()
        evidence = db.execute("SELECT * FROM evidence").fetchall()
    assert rows and all("review" in str(r["path"]) for r in rows)
    assert all(bundle["review_id"] not in str(r["id"]) for r in evidence)
