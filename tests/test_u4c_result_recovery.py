from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from paperforge.api import PaperForgeService
from paperforge.result_update import apply_proposal, approve_proposal, build_proposal, reconcile_result_updates
from test_u24a_finalization import _fact, _spec_with_table


def prepared(root: Path):
    service, spec, block, _ = _spec_with_table(root, "Old result 20.0 ms.")
    old = (root / "paper.tex").read_bytes()
    proposal = build_proposal(root, service.memory, spec,
                              proposed_block=block.replace("20.0", "22.4"), facts_used=[_fact()])
    approval = approve_proposal(root, service.memory, proposal["proposal_id"], approver="test-owner")
    new = old.replace(block.encode(), proposal["proposed_block"].encode(), 1)
    return service, proposal, approval, old, new


def set_state(service, proposal, state):
    with service.memory.connect() as db:
        db.execute("UPDATE result_updates SET state=? WHERE id=?", (state, proposal["proposal_id"]))


def verdict(root, service, *, repair=False):
    return reconcile_result_updates(root, service.memory, repair=repair)["findings"][0]["verdict"]


@pytest.mark.parametrize("case", range(1, 9))
def test_ru_crash_matrix(tmp_path, case):
    service, proposal, approval, old, new = prepared(tmp_path)
    base = tmp_path / ".paperforge/result_updates" / proposal["proposal_id"]
    if case == 1:
        assert verdict(tmp_path, service) == "CLEAN"
        apply_proposal(tmp_path, service.memory, proposal["proposal_id"])
    elif case == 2:
        set_state(service, proposal, "APPLYING")
        assert verdict(tmp_path, service) == "RECOVERABLE_ROLLBACK"
        verdict(tmp_path, service, repair=True)
        assert (tmp_path / "paper.tex").read_bytes() == old
    elif case in {3, 7, 8}:
        set_state(service, proposal, "APPLYING")
        (tmp_path / "paper.tex").write_bytes(new)
        if case == 7:
            with service.memory.connect() as db:
                db.execute("UPDATE result_approvals SET status='invalidated'")
            assert verdict(tmp_path, service, repair=True) == "MANUAL_INTERVENTION_REQUIRED"
            assert not (base / "applied.json").exists()
            assert (tmp_path / "paper.tex").read_bytes() == new
            return
        assert verdict(tmp_path, service) == "RECOVERABLE_FORWARD"
        verdict(tmp_path, service, repair=True)
    elif case in {4, 5, 6}:
        apply_proposal(tmp_path, service.memory, proposal["proposal_id"])
        if case == 4:
            (base / "applied.json").unlink()
        elif case == 5:
            with service.memory.connect() as db:
                db.execute("DELETE FROM result_baselines")
        else:
            # Keep the expected new block, but change bytes outside it.
            unknown = new.replace(b"Intro.", b"Manual changes.")
            (tmp_path / "paper.tex").write_bytes(unknown)
            assert verdict(tmp_path, service, repair=True) == "MANUAL_INTERVENTION_REQUIRED"
            assert (tmp_path / "paper.tex").read_bytes() == unknown
            return
        assert verdict(tmp_path, service) == "RECOVERABLE_FORWARD"
        verdict(tmp_path, service, repair=True)
    if case != 2:
        assert (tmp_path / "paper.tex").read_bytes() == new
        with service.memory.connect() as db:
            baseline = db.execute("SELECT * FROM result_baselines").fetchone()
        assert baseline["approval_id"] == approval["approval_id"]
        assert json.loads((base / "applied.json").read_bytes())["new_source_sha256"] == baseline["source_sha256"]
    before = (tmp_path / "paper.tex").read_bytes()
    assert verdict(tmp_path, PaperForgeService(tmp_path), repair=True) == "CLEAN"
    assert verdict(tmp_path, PaperForgeService(tmp_path), repair=True) == "CLEAN"
    assert (tmp_path / "paper.tex").read_bytes() == before


@pytest.mark.parametrize("damage", ["artifact", "approval_file", "proposal", "unapproved", "third_state"])
def test_forward_recovery_cannot_bypass_binding(tmp_path, damage):
    service, proposal, _, old, new = prepared(tmp_path)
    base = tmp_path / ".paperforge/result_updates" / proposal["proposal_id"]
    set_state(service, proposal, "APPLYING")
    (tmp_path / "paper.tex").write_bytes(new)
    if damage == "artifact":
        (tmp_path / "artifacts/tables/table-1/table.tex").write_text("stale artifact")
    elif damage == "approval_file":
        (base / "approval.json").unlink()
    elif damage == "proposal":
        body = json.loads((base / "proposal.json").read_bytes())
        body["proposed_block"] += "tampered"
        (base / "proposal.json").write_text(json.dumps(body))
    elif damage == "unapproved":
        with service.memory.connect() as db:
            db.execute("DELETE FROM result_approvals")
    elif damage == "third_state":
        (tmp_path / "paper.tex").write_bytes(new + b"manual suffix")
    before = (tmp_path / "paper.tex").read_bytes()
    with service.memory.connect() as db:
        domain_before = list(db.iterdump())
    assert verdict(tmp_path, service, repair=True) == "MANUAL_INTERVENTION_REQUIRED"
    assert (tmp_path / "paper.tex").read_bytes() == before
    with service.memory.connect() as db:
        assert list(db.iterdump()) == domain_before


def test_exception_after_replace_does_not_claim_rollback(tmp_path, monkeypatch):
    import paperforge.result_update as module

    service, proposal, _, _, new = prepared(tmp_path)
    original = module.atomic_write_text

    def failing_write(workspace, path, text, **kwargs):
        if str(path).endswith("applied.json"):
            raise OSError("injected audit failure after target replace")
        return original(workspace, path, text, **kwargs)

    monkeypatch.setattr(module, "atomic_write_text", failing_write)
    with pytest.raises(OSError):
        apply_proposal(tmp_path, service.memory, proposal["proposal_id"])
    assert (tmp_path / "paper.tex").read_bytes() == new
    with service.memory.connect() as db:
        assert db.execute("SELECT state FROM result_updates").fetchone()[0] != "ROLLED_BACK"
    monkeypatch.setattr(module, "atomic_write_text", original)
    assert verdict(tmp_path, service) == "RECOVERABLE_FORWARD"
    verdict(tmp_path, service, repair=True)
    assert verdict(tmp_path, service) == "CLEAN"


@pytest.mark.parametrize("field,value", [("approval_id", "nonexistent"), ("old_source_sha256", "0" * 64),
                                         ("publication_ready", True), ("scientific_review_state", "reviewed"),
                                         ("applied_at", "different-time")])
def test_corrupt_applied_audit_cannot_be_clean_or_returned(tmp_path, field, value):
    service, proposal, _, _, new = prepared(tmp_path)
    apply_proposal(tmp_path, service.memory, proposal["proposal_id"])
    path = tmp_path / ".paperforge/result_updates" / proposal["proposal_id"] / "applied.json"
    payload = json.loads(path.read_bytes())
    payload[field] = value
    path.write_text(json.dumps(payload))
    before = path.read_bytes()
    assert verdict(tmp_path, service, repair=True) == "MANUAL_INTERVENTION_REQUIRED"
    with pytest.raises(Exception, match="reconciliation"):
        apply_proposal(tmp_path, service.memory, proposal["proposal_id"])
    assert path.read_bytes() == before
    assert (tmp_path / "paper.tex").read_bytes() == new


def test_stale_ledger_blocks_forward_recovery(tmp_path):
    from paperforge.result_ledger import ResultLedgerQuery, freeze_ledger

    service, spec, old, _ = _spec_with_table(tmp_path, "Old result 20.0 ms.")
    eid = service.memory.add_evidence(evidence_type="TEST", excerpt="latency", metadata={
        "measurement_kind": "measured", "acquisition_origin": "paperforge_run", "environment_role": "scientific",
        "metric_name": "latency_ms", "value_str": "22.4", "unit": "ms", "run_identity": {"schema": "paperforge.scientific-run-identity/v1"}})
    ledger = freeze_ledger(tmp_path, service.memory, ResultLedgerQuery(
        purpose="scientific", metric_names=["latency_ms"], evidence_ids=(eid,)))
    spec = replace(spec, ledger_bindings=({"ledger_id": ledger["ledger_id"], "ledger_digest": ledger["ledger_digest"]},))
    proposal = build_proposal(tmp_path, service.memory, spec, proposed_block=old.replace("20.0", "22.4"), facts_used=[_fact()])
    approve_proposal(tmp_path, service.memory, proposal["proposal_id"], approver="owner")
    set_state(service, proposal, "APPLYING")
    new = (tmp_path / "paper.tex").read_text().replace(old, proposal["proposed_block"], 1)
    (tmp_path / "paper.tex").write_text(new)
    with service.memory.connect() as db:
        db.execute("DELETE FROM evidence WHERE id=?", (eid,))
    assert verdict(tmp_path, service, repair=True) == "MANUAL_INTERVENTION_REQUIRED"
    assert (tmp_path / "paper.tex").read_text() == new
