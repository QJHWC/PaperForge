from __future__ import annotations

import json
from enum import Enum

import pytest
from test_u4c_result_recovery import prepared, set_state
from test_u22_import_transaction import _approve_needs_human, _stage

from paperforge.api import PaperForgeService
from paperforge.external_import import ApprovalError, materialize_import, reconcile_imports
from paperforge.models import WorkflowStatus
from paperforge.result_update import reconcile_result_updates


class RecoveryOutcome(str, Enum):
    CLEAN = "CLEAN"
    RECOVERABLE_FORWARD = "RECOVERABLE_FORWARD"
    RECOVERABLE_ROLLBACK = "RECOVERABLE_ROLLBACK"
    MANUAL_INTERVENTION_REQUIRED = "MANUAL_INTERVENTION_REQUIRED"
    UNRECOVERABLE = "UNRECOVERABLE"


@pytest.mark.parametrize("state", ["needs_human", "approved", "running"])
def test_workflow_restart_preserves_domain_and_status_reads_are_readonly(tmp_path, state):
    from frontend.process_manager import ProcessManager

    service = PaperForgeService(tmp_path)
    if state in {"needs_human", "approved"}:
        handle = service.run(profile="full", inputs={})
        run_id = handle.run_id
        if state == "approved":
            proposal_id = service.workflow.get(run_id)["metadata"]["proposal_id"]
            service.approve(proposal_id, approved_by="test-owner")
            approved = service.workflow.require_approval(proposal_id)
            assert approved["status"] == "APPROVED"
        expected = "AWAITING_APPROVAL"
    else:
        run_id = service.workflow.create("writing-only")
        service.workflow.transition(run_id, WorkflowStatus.RUNNING)
        expected = "RUNNING"
    reopened = PaperForgeService(tmp_path)
    process_manager = ProcessManager(root=tmp_path, results_dir=tmp_path / "results")
    assert process_manager._runs == {}
    if state == "approved":
        assert reopened.workflow.require_approval(proposal_id) == approved
    with reopened.memory.connect() as db:
        before = list(db.iterdump())
    for _ in range(20):
        assert reopened.status(run_id).status == expected
        unified = reopened.unified_status(run_id)["unified"]
        assert unified["workflow_state"] == expected
        assert unified["process_state"] == "UNKNOWN_NOT_ATTACHED"
        assert unified["process_domain_relation"] == "process_unknown"
        assert unified["workflow_state"] != "COMPLETED"
    with reopened.memory.connect() as db:
        assert list(db.iterdump()) == before


@pytest.mark.parametrize("damage", ["orphan_evidence", "missing_evidence", "wrong_key", "empty_materialization", "content", "source_hash", "malformed_ids"])
def test_import_ambiguous_state_is_retained_without_fabrication(tmp_path, damage):
    service, record = _stage(tmp_path)
    _approve_needs_human(tmp_path, service, record)
    result = materialize_import(tmp_path, service.memory, record.import_id)
    with service.memory.connect() as db:
        if damage == "orphan_evidence":
            db.execute("DELETE FROM import_materializations")
        elif damage == "missing_evidence":
            db.execute("DELETE FROM evidence WHERE id=?", (result["evidence_ids"][0],))
        elif damage == "wrong_key":
            row = db.execute("SELECT metadata_json FROM evidence WHERE id=?", (result["evidence_ids"][0],)).fetchone()
            meta = json.loads(row[0])
            meta["materialization_key"] = "unknown"
            db.execute("UPDATE evidence SET metadata_json=? WHERE id=?", (json.dumps(meta), result["evidence_ids"][0]))
        elif damage == "empty_materialization":
            db.execute("UPDATE import_materializations SET evidence_ids_json='[]'")
        elif damage == "content":
            db.execute("UPDATE evidence SET excerpt='tampered scientific result'")
        elif damage == "malformed_ids":
            db.execute("UPDATE import_materializations SET evidence_ids_json='[{}]'")
        else:
            row = db.execute("SELECT metadata_json FROM evidence").fetchone()
            meta = json.loads(row[0])
            meta["source_sha256"] = "0" * 64
            db.execute("UPDATE evidence SET metadata_json=?", (json.dumps(meta),))
        before = list(db.iterdump())
    for _ in range(2):
        report = reconcile_imports(tmp_path, PaperForgeService(tmp_path).memory, repair=True)
        assert RecoveryOutcome(report["recovery_outcome"]) == RecoveryOutcome.MANUAL_INTERVENTION_REQUIRED
        assert report["repaired"] is False
    with service.memory.connect() as db:
        assert list(db.iterdump()) == before
    with pytest.raises(ApprovalError, match="integrity mismatch"):
        materialize_import(tmp_path, service.memory, record.import_id)


def test_import_idempotency_stress_20_reopens(tmp_path):
    service, record = _stage(tmp_path)
    _approve_needs_human(tmp_path, service, record)
    first = materialize_import(tmp_path, service.memory, record.import_id)
    with service.memory.connect() as db:
        before = list(db.iterdump())
    for _ in range(20):
        reopened = PaperForgeService(tmp_path)
        result = materialize_import(tmp_path, reopened.memory, record.import_id)
        assert result["idempotent"]
        assert result["evidence_ids"] == first["evidence_ids"]
        assert RecoveryOutcome(reconcile_imports(tmp_path, reopened.memory, repair=True)["recovery_outcome"]) == RecoveryOutcome.CLEAN
    with service.memory.connect() as db:
        assert list(db.iterdump()) == before
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_result_update_recovery_stress_20_fresh_workspaces(tmp_path):
    for iteration in range(20):
        root = tmp_path / str(iteration)
        root.mkdir()
        service, proposal, _, _, new = prepared(root)
        set_state(service, proposal, "APPLYING")
        (root / "paper.tex").write_bytes(new)
        reopened = PaperForgeService(root)
        report = reconcile_result_updates(root, reopened.memory, repair=True)
        assert RecoveryOutcome(report["findings"][0]["verdict"]) == RecoveryOutcome.RECOVERABLE_FORWARD
        with reopened.memory.connect() as db:
            before = list(db.iterdump())
        for _ in range(2):
            assert reconcile_result_updates(root, reopened.memory, repair=True)["consistent"]
        assert (root / "paper.tex").read_bytes() == new
        with reopened.memory.connect() as db:
            assert list(db.iterdump()) == before
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not list(root.rglob("*.tmp"))
        assert not list(root.rglob("*.partial"))
        assert not list(root.rglob(".*.json.*"))
        assert not list(root.rglob(".paper.tex.*"))


def test_sqlite_durability_audit_and_rollback(tmp_path):
    service = PaperForgeService(tmp_path)
    with service.memory.connect() as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL on this configured connection
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(RuntimeError, match="rollback"), service.memory.connect() as db:
        db.execute("INSERT INTO metadata (key,value) VALUES ('u4c-rollback','uncommitted')")
        raise RuntimeError("rollback")
    with PaperForgeService(tmp_path).memory.connect() as db:
        assert db.execute("SELECT value FROM metadata WHERE key='u4c-rollback'").fetchone() is None
