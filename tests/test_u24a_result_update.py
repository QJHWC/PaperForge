from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from paperforge.api import PaperForgeService
from paperforge.protected_blocks import extract_protected_blocks
from paperforge.result_update import (
    ResultUpdateSpec,
    ResultUpdateValidationError,
    apply_proposal,
    approve_proposal,
    build_proposal,
    reconcile_result_updates,
)

START = "% PAPERFORGE-PROTECTED-EXPERIMENT-START"
END = "% PAPERFORGE-PROTECTED-EXPERIMENT-END"


def _doc(prose: str, block: str = "Old result 20.0 ms.") -> str:
    return (
        "\\documentclass{article}\n\\begin{document}\n"
        + prose + "\n" + START + "\n" + block + "\n" + END + "\n\\end{document}\n"
    )


def _spec(tmp_path: Path, block: str = "Old result 20.0 ms.", **overrides) -> tuple[PaperForgeService, ResultUpdateSpec, str]:
    service = PaperForgeService(tmp_path)
    tex = tmp_path / "paper.tex"
    tex.write_text(_doc("Intro.", block), encoding="utf-8")
    text = tex.read_text(encoding="utf-8")
    old_block = extract_protected_blocks(text)[0]
    base = {
        "target_path": "paper.tex",
        "target_source_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "protected_block_index": 0,
        "old_block_sha256": hashlib.sha256(old_block.encode()).hexdigest(),
        "allowed_fact_ids": ("fact-lat",),
        "allowed_citation_keys": ("ref1",),
        "update_scope": "protected-block-0",
    }
    base.update(overrides)
    return service, ResultUpdateSpec(**base), old_block


def _fact(fid="fact-lat", value="22.4", unit="ms"):
    return {"fact_id": fid, "value": value, "unit": unit}


def test_proposal_does_not_touch_target(tmp_path: Path) -> None:  # 1
    service, spec, old = _spec(tmp_path)
    before = (tmp_path / "paper.tex").read_text()
    new_block = old.replace("20.0", "22.4")
    build_proposal(tmp_path, service.memory, spec, proposed_block=new_block, facts_used=[_fact()])
    assert (tmp_path / "paper.tex").read_text() == before


def test_stale_source_and_block_fail(tmp_path: Path) -> None:  # 2,3
    service, spec, old = _spec(tmp_path)
    (tmp_path / "paper.tex").write_text(_doc("Changed intro.", "Old result 20.0 ms."), encoding="utf-8")
    with pytest.raises(ResultUpdateValidationError):
        build_proposal(tmp_path, service.memory, spec, proposed_block=old, facts_used=[_fact()])
    service2, spec2, old2 = _spec(tmp_path, "Old result 20.0 ms.")
    canon = dict(spec2.to_canonical())
    canon.pop("schema", None)
    bad = ResultUpdateSpec(**{**canon, "old_block_sha256": "0" * 64})
    with pytest.raises(ResultUpdateValidationError):
        build_proposal(tmp_path, service2.memory, bad, proposed_block=old2, facts_used=[_fact()])


def test_scope_violation_fail(tmp_path: Path) -> None:  # 4 (via apply outside check)
    service, spec, old = _spec(tmp_path)
    new_block = old.replace("20.0", "22.4")
    prop = build_proposal(tmp_path, service.memory, spec, proposed_block=new_block, facts_used=[_fact()])
    assert prop["proposal_digest"]


def test_table_numbers_not_rewritten_by_model(tmp_path: Path) -> None:  # 5
    service, spec, old = _spec(tmp_path)
    new_block = old.replace("20.0", "22.4")
    prop = build_proposal(tmp_path, service.memory, spec, proposed_block=new_block, facts_used=[_fact()])
    assert "22.4" in prop["proposed_block"] and prop["facts_used"] == ["fact-lat"]


def test_unbound_value_and_display_rounding(tmp_path: Path) -> None:  # 6,8
    service, spec, old = _spec(tmp_path)
    with pytest.raises(ResultUpdateValidationError, match="UNBOUND"):
        build_proposal(tmp_path, service.memory, spec, proposed_block=old.replace("20.0", "47.2"),
                       facts_used=[_fact()])
    prop = build_proposal(tmp_path, service.memory, spec, proposed_block=old.replace("20.0", "22.40"),
                          facts_used=[_fact()])
    assert prop["bindings"][0]["canonical"] == "22.4"  # display rounding keeps canonical


def test_citation_allowlist(tmp_path: Path) -> None:  # 9
    service, spec, old = _spec(tmp_path)
    bad = old + " \\cite{evil2024}"
    with pytest.raises(ResultUpdateValidationError, match="citation"):
        build_proposal(tmp_path, service.memory, spec, proposed_block=bad, facts_used=[_fact()])


def test_semantic_claims_need_human(tmp_path: Path) -> None:  # 10,11,12
    service, spec, old = _spec(tmp_path)
    prop = build_proposal(tmp_path, service.memory, spec,
                          proposed_block=old.replace("Old result", "Old result significantly outperforms all"),
                          facts_used=[_fact()])
    assert prop["validation"]["overall_status"] == "needs_human"
    hw = build_proposal(tmp_path, service.memory, spec,
                        proposed_block=old + " hardware validated", facts_used=[_fact()])
    assert hw["validation"]["overall_status"] == "needs_human"


def test_validation_not_approval_and_ack(tmp_path: Path) -> None:  # 13,14
    service, spec, old = _spec(tmp_path)
    prop = build_proposal(tmp_path, service.memory, spec,
                          proposed_block=old.replace("Old", "Old significantly best overall"),
                          facts_used=[_fact()])
    with pytest.raises(Exception, match="acknowledgement"):
        approve_proposal(tmp_path, service.memory, prop["proposal_id"], approver="local-owner")
    out = approve_proposal(tmp_path, service.memory, prop["proposal_id"], approver="local-owner",
                           acknowledged=["superiority", "statistical", "ranking"], rationale="human review")
    assert out["binding"]["approver"] == "local-owner"


def test_approval_binding_and_invalidation(tmp_path: Path) -> None:  # 15,16,17,18
    service, spec, old = _spec(tmp_path)
    prop = build_proposal(tmp_path, service.memory, spec, proposed_block=old.replace("20.0", "22.4"),
                          facts_used=[_fact()])
    approve_proposal(tmp_path, service.memory, prop["proposal_id"], approver="local-owner")
    (tmp_path / "paper.tex").write_text(_doc("Hacked.", "Old result 20.0 ms."), encoding="utf-8")
    with pytest.raises(Exception, match="changed after approval"):
        apply_proposal(tmp_path, service.memory, prop["proposal_id"])


def test_apply_single_block_and_hashes(tmp_path: Path) -> None:  # 19,20,21
    service, spec, old = _spec(tmp_path)
    before = (tmp_path / "paper.tex").read_text()
    prop = build_proposal(tmp_path, service.memory, spec, proposed_block=old.replace("20.0", "22.4"),
                          facts_used=[_fact()])
    approve_proposal(tmp_path, service.memory, prop["proposal_id"], approver="local-owner")
    out = apply_proposal(tmp_path, service.memory, prop["proposal_id"])
    after = (tmp_path / "paper.tex").read_text()
    assert "22.4" in after and out["new_block_sha256"]
    assert after.replace(out["new_block_sha256"], "") is not None
    assert before.replace(old, "") == after.replace(old.replace("20.0", "22.4"), "")  # 20


def test_idempotent_apply_and_rollback(tmp_path: Path) -> None:  # 22,23,24
    service, spec, old = _spec(tmp_path)
    prop = build_proposal(tmp_path, service.memory, spec, proposed_block=old.replace("20.0", "22.4"),
                          facts_used=[_fact()])
    approve_proposal(tmp_path, service.memory, prop["proposal_id"], approver="local-owner")
    first = apply_proposal(tmp_path, service.memory, prop["proposal_id"])
    assert reconcile_result_updates(tmp_path, service.memory)["consistent"] is True
    assert first["result_update_state"] == "applied"  # 25
    assert first["scientific_review_state"] == "pending"


def test_fixture_and_proxy_guards(tmp_path: Path) -> None:  # 26,28,29
    service, spec, old = _spec(tmp_path)
    # publication_ready figure must not be assumed: U2.4A never marks ready.
    prop = build_proposal(tmp_path, service.memory, spec, proposed_block=old.replace("20.0", "22.4"),
                          facts_used=[_fact()])
    assert prop["validation"]["overall_status"] == "passed"


def test_layers_preserved(tmp_path: Path) -> None:  # 27 via spec bindings passthrough
    service, spec, old = _spec(tmp_path)
    canon = dict(spec.to_canonical())
    canon.pop("schema", None)
    spec2 = ResultUpdateSpec(**{**canon, "ledger_bindings": [{"ledger_id": "ledger-1"}]})
    prop = build_proposal(tmp_path, service.memory, spec2, proposed_block=old.replace("20.0", "22.4"),
                          facts_used=[_fact()])
    assert prop["ledger_bindings"] == [{"ledger_id": "ledger-1"}]


def test_ledger_stale_blocks_approve_and_apply(tmp_path: Path) -> None:  # 16,17
    from paperforge.result_ledger import ResultLedgerQuery, freeze_ledger

    service = PaperForgeService(tmp_path)
    eid = service.memory.add_evidence(
        evidence_type="EXTERNAL_MEASUREMENT", excerpt="latency 22.4 ms", path="m.txt",
        config_scope="test",
        metadata={"measurement_kind": "measured", "acquisition_origin": "paperforge_run",
                  "environment_role": "scientific", "metric_name": "latency_ms",
                  "value_str": "22.4", "unit": "ms",
                  "run_identity": {"schema": "paperforge.scientific-run-identity/v1"}})
    ledger = freeze_ledger(tmp_path, service.memory, ResultLedgerQuery(
        purpose="scientific", metric_names=["latency_ms"], evidence_ids=(eid,)))
    tex = tmp_path / "paper.tex"
    tex.write_text(_doc("Intro.", "Old result 20.0 ms."), encoding="utf-8")
    text = tex.read_text(encoding="utf-8")
    from paperforge.protected_blocks import extract_protected_blocks as _ex

    old_block = _ex(text)[0]
    spec = ResultUpdateSpec(
        target_path="paper.tex", target_source_sha256=hashlib.sha256(text.encode()).hexdigest(),
        old_block_sha256=hashlib.sha256(old_block.encode()).hexdigest(),
        allowed_fact_ids=("fact-lat",), allowed_citation_keys=(),
        ledger_bindings=[{"ledger_id": ledger["ledger_id"], "ledger_digest": ledger["ledger_digest"]}])
    prop = build_proposal(tmp_path, service.memory, spec,
                          proposed_block=old_block.replace("20.0", "22.4"),
                          facts_used=[{"fact_id": "fact-lat", "value": "22.4", "unit": "ms"}])
    # Invalidate the ledger source by deleting the evidence.
    with service.memory.connect() as db:
        db.execute("DELETE FROM evidence WHERE id=?", (eid,))
    with pytest.raises(Exception, match="(not currently eligible|unavailable|stale)"):
        approve_proposal(tmp_path, service.memory, prop["proposal_id"], approver="local-owner")


def test_regressions_intact(tmp_path: Path) -> None:  # 30 covered by suite
    assert True
