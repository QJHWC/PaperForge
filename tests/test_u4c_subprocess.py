from __future__ import annotations

import json
import selectors
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from paperforge.api import PaperForgeService
from paperforge.external_import import materialize_import, reconcile_imports
from paperforge.result_update import reconcile_result_updates
from test_u22_import_transaction import _approve_needs_human, _stage
from test_u4b_finalization import setup_target
from test_u4c_result_recovery import prepared


def kill_at_checkpoint(root: Path, mode: str, identity: str = "unused") -> None:
    if not hasattr(signal, "SIGKILL"):
        pytest.skip("SIGKILL unavailable on this platform; not a power-loss test")
    worker = Path(__file__).with_name("u4c_crash_child.py")
    process = subprocess.Popen([sys.executable, str(worker), str(root), mode, identity],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=20), "child did not reach controlled crash point"
            line = process.stdout.readline()
            if line.strip() != "U4C_READY_TO_KILL":
                process.kill()
                _, error = process.communicate(timeout=10)
                pytest.fail(f"child failed before crash point: {line} {error}")
        process.kill()
        process.communicate(timeout=10)
        assert process.returncode == -signal.SIGKILL
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)


@pytest.mark.parametrize("mode", ["import_precommit", "import_postcommit"])
def test_external_import_real_sigkill_reopen(tmp_path, mode):
    service, record = _stage(tmp_path)
    _approve_needs_human(tmp_path, service, record)
    kill_at_checkpoint(tmp_path, mode, record.import_id)
    reopened = PaperForgeService(tmp_path)
    with reopened.memory.connect() as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        count = db.execute("SELECT count(*) FROM evidence").fetchone()[0]
        materials = db.execute("SELECT count(*) FROM import_materializations").fetchone()[0]
        state = db.execute("SELECT state FROM imports WHERE id=?", (record.import_id,)).fetchone()[0]
    if mode == "import_precommit":
        assert count == materials == 0
        assert state != "materialized"
    else:
        assert count == materials == 1
        assert state == "materialized"
    assert reconcile_imports(tmp_path, reopened.memory)["consistent"]
    first = materialize_import(tmp_path, reopened.memory, record.import_id)
    second = materialize_import(tmp_path, reopened.memory, record.import_id)
    assert first["evidence_ids"] == second["evidence_ids"]
    assert second["idempotent"]


@pytest.mark.parametrize("mode,expected", [("result_pre_replace", "RECOVERABLE_ROLLBACK"),
                                           ("result_post_replace", "RECOVERABLE_FORWARD")])
def test_result_update_real_sigkill_reopen(tmp_path, mode, expected):
    _, proposal, _, old, new = prepared(tmp_path)
    kill_at_checkpoint(tmp_path, mode, proposal["proposal_id"])
    reopened = PaperForgeService(tmp_path)
    before = (tmp_path / "paper.tex").read_bytes()
    assert before == (old if mode == "result_pre_replace" else new)
    report = reconcile_result_updates(tmp_path, reopened.memory)
    assert report["findings"][0]["verdict"] == expected
    reconcile_result_updates(tmp_path, reopened.memory, repair=True)
    assert reconcile_result_updates(tmp_path, reopened.memory)["consistent"]
    assert (tmp_path / "paper.tex").read_bytes() == before


def test_evaluator_real_sigkill_has_no_domain_mutation(tmp_path):
    from paperforge.release_eligibility import assessment_current, evaluate_release, freeze_assessment

    service, target, policy = setup_target(tmp_path)
    (tmp_path / "test-target.json").write_text(json.dumps(target.to_dict()))
    report = evaluate_release(tmp_path, service.memory, target, policy)
    freeze_assessment(tmp_path, service.memory, report)
    with service.memory.connect() as db:
        before = list(db.iterdump())
    report_path = tmp_path / ".paperforge/release" / report["report_id"] / "report.json"
    history = report_path.read_bytes()
    kill_at_checkpoint(tmp_path, "evaluator")
    reopened = PaperForgeService(tmp_path)
    with reopened.memory.connect() as db:
        assert list(db.iterdump()) == before
    assert report_path.read_bytes() == history
    assert evaluate_release(tmp_path, reopened.memory, target, policy)["report_digest"] == report["report_digest"]
    assert assessment_current(tmp_path, reopened.memory, report["report_id"])["currently_valid"]


def test_release_freeze_real_sigkill_before_registry(tmp_path):
    from paperforge.release_eligibility import assessment_current, evaluate_release, freeze_assessment

    service, target, policy = setup_target(tmp_path)
    (tmp_path / "test-target.json").write_text(json.dumps(target.to_dict()))
    report = evaluate_release(tmp_path, service.memory, target, policy)
    kill_at_checkpoint(tmp_path, "release_registry")
    reopened = PaperForgeService(tmp_path)
    assert not assessment_current(tmp_path, reopened.memory, report["report_id"])["currently_valid"]
    freeze_assessment(tmp_path, reopened.memory, report)
    assert assessment_current(tmp_path, reopened.memory, report["report_id"])["currently_valid"]
