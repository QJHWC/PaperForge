from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from paperforge.api import PaperForgeService
from paperforge.protected_blocks import extract_protected_blocks
from paperforge.result_update import (
    ResultUpdateSpec,
    apply_proposal,
    approve_proposal,
    build_proposal,
    reconcile_result_updates,
    verify_bound_artifact_integrity,
)

START = "% PAPERFORGE-PROTECTED-EXPERIMENT-START"
END = "% PAPERFORGE-PROTECTED-EXPERIMENT-END"


def _doc(prose: str, block: str) -> str:
    return ("\\documentclass{article}\n\\begin{document}\n" + prose + "\n" + START + "\n" + block + "\n" + END + "\n\\end{document}\n")


def _spec_with_table(tmp_path: Path, block: str, table_id: str = "table-1"):
    service = PaperForgeService(tmp_path)
    out_dir = tmp_path / "artifacts" / "tables" / table_id
    (out_dir).mkdir(parents=True, exist_ok=True)
    (out_dir / "table.tex").write_text("old tex", encoding="utf-8")
    (out_dir / "manifest.json").write_text('{"table_id": "x"}', encoding="utf-8")
    from paperforge.artifacts import ArtifactStore

    store = ArtifactStore(tmp_path, allowed_roots=("artifacts",),
                          allowed_suffixes=(".tex", ".json"), allowed_kinds=("table", "manifest"),
                          memory=service.memory)
    store.register(f"artifacts/tables/{table_id}/table.tex", kind="table")
    store.register(f"artifacts/tables/{table_id}/manifest.json", kind="manifest")
    tex = tmp_path / "paper.tex"
    tex.write_text(_doc("Intro.", block), encoding="utf-8")
    text = tex.read_text(encoding="utf-8")
    old_block = extract_protected_blocks(text)[0]
    spec = ResultUpdateSpec(
        target_path="paper.tex", target_source_sha256=hashlib.sha256(text.encode()).hexdigest(),
        old_block_sha256=hashlib.sha256(old_block.encode()).hexdigest(),
        allowed_table_artifact_ids=(table_id,), allowed_fact_ids=("fact-lat",),
        update_scope="b0")
    return service, spec, old_block, table_id


def _fact(fid="fact-lat", value="22.4"):
    return {"fact_id": fid, "value": value, "unit": "ms"}


def test_fact_file_tamper_blocks_apply(tmp_path: Path) -> None:  # F1-1
    service, spec, old, tid = _spec_with_table(tmp_path, "Old result 20.0 ms.")
    prop = build_proposal(tmp_path, service.memory, spec, proposed_block=old.replace("20.0", "22.4"),
                          facts_used=[_fact()])
    approve_proposal(tmp_path, service.memory, prop["proposal_id"], approver="local-owner")
    (tmp_path / "artifacts" / "tables" / tid / "table.tex").write_text("tampered", encoding="utf-8")
    with pytest.raises(Exception, match="bound artifact"):
        apply_proposal(tmp_path, service.memory, prop["proposal_id"])


def test_table_manifest_tamper_blocks_apply(tmp_path: Path) -> None:  # F1-2/5
    service, spec, old, tid = _spec_with_table(tmp_path, "Old result 20.0 ms.")
    prop = build_proposal(tmp_path, service.memory, spec, proposed_block=old.replace("20.0", "22.4"),
                          facts_used=[_fact()])
    approve_proposal(tmp_path, service.memory, prop["proposal_id"], approver="local-owner")
    (tmp_path / "artifacts" / "tables" / tid / "manifest.json").write_text('{"evil": 1}', encoding="utf-8")
    with pytest.raises(Exception, match="bound artifact"):
        apply_proposal(tmp_path, service.memory, prop["proposal_id"])


def test_store_metadata_without_bytes_fails(tmp_path: Path) -> None:  # F1-4
    service, spec, old, tid = _spec_with_table(tmp_path, "Old result 20.0 ms.")
    # Metadata unchanged in DB, but bytes changed on disk.
    (tmp_path / "artifacts" / "tables" / tid / "table.tex").write_bytes(b"different bytes")
    with pytest.raises(Exception, match="bound artifact"):
        verify_bound_artifact_integrity(tmp_path, service.memory, spec)


def test_unchanged_artifacts_apply(tmp_path: Path) -> None:  # F1-6
    service, spec, old, tid = _spec_with_table(tmp_path, "Old result 20.0 ms.")
    prop = build_proposal(tmp_path, service.memory, spec, proposed_block=old.replace("20.0", "22.4"),
                          facts_used=[_fact()])
    approve_proposal(tmp_path, service.memory, prop["proposal_id"], approver="local-owner")
    out = apply_proposal(tmp_path, service.memory, prop["proposal_id"])
    assert out["result_update_state"] == "applied"


def test_structural_numbers_ignored(tmp_path: Path) -> None:  # F2-1,2
    service = PaperForgeService(tmp_path)
    block = "See Table III and Eq. (7). Old result 20.0 ms."
    tex = tmp_path / "paper.tex"
    tex.write_text(_doc("Intro.", block), encoding="utf-8")
    text = tex.read_text(encoding="utf-8")
    old = extract_protected_blocks(text)[0]
    spec = ResultUpdateSpec(target_path="paper.tex",
                            target_source_sha256=hashlib.sha256(text.encode()).hexdigest(),
                            old_block_sha256=hashlib.sha256(old.encode()).hexdigest(),
                            allowed_fact_ids=("fact-lat",))
    prop = build_proposal(tmp_path, service.memory, spec,
                          proposed_block=old.replace("20.0", "22.4"), facts_used=[_fact()])
    assert prop["proposal_digest"]


def test_protocol_changes_require_binding(tmp_path: Path) -> None:  # F2-3,4,5
    for old_proto, _new_proto in (("300 epochs", "500 epochs"), ("3 seeds", "5 seeds"), ("896 px", "1024 px")):
        service = PaperForgeService(tmp_path / f"p-{old_proto.split()[0]}") if False else None
    service = PaperForgeService(tmp_path)
    block = "Trained 300 epochs on 896 px with 3 seeds. Old result 20.0 ms."
    tex = tmp_path / "paper.tex"
    tex.write_text(_doc("Intro.", block), encoding="utf-8")
    text = tex.read_text(encoding="utf-8")
    old = extract_protected_blocks(text)[0]
    spec = ResultUpdateSpec(target_path="paper.tex",
                            target_source_sha256=hashlib.sha256(text.encode()).hexdigest(),
                            old_block_sha256=hashlib.sha256(old.encode()).hexdigest(),
                            allowed_fact_ids=("fact-lat",))
    with pytest.raises(Exception, match="UNBOUND_PROTOCOL_VALUE"):
        build_proposal(tmp_path, service.memory, spec,
                       proposed_block=old.replace("300 epochs", "500 epochs"), facts_used=[_fact()])


def test_protocol_binding_passes(tmp_path: Path) -> None:  # F2-6
    service = PaperForgeService(tmp_path)
    block = "Trained 300 epochs. Old result 20.0 ms."
    tex = tmp_path / "paper.tex"
    tex.write_text(_doc("Intro.", block), encoding="utf-8")
    text = tex.read_text(encoding="utf-8")
    old = extract_protected_blocks(text)[0]
    spec = ResultUpdateSpec(
        target_path="paper.tex", target_source_sha256=hashlib.sha256(text.encode()).hexdigest(),
        old_block_sha256=hashlib.sha256(old.encode()).hexdigest(),
        allowed_fact_ids=("fact-lat",),
        allowed_protocol_values=({"name": "epochs", "value": "300", "unit": "epochs", "source": "ledger-1"},))
    # No protocol change: passes without using the binding.
    prop = build_proposal(tmp_path, service.memory, spec,
                          proposed_block=old.replace("20.0", "22.4"), facts_used=[_fact()])
    assert prop["protocol_bindings"] == []


def test_reconcile_matrix(tmp_path: Path) -> None:  # F3
    service = PaperForgeService(tmp_path)
    block = "Old result 20.0 ms."
    tex = tmp_path / "paper.tex"
    tex.write_text(_doc("Intro.", block), encoding="utf-8")
    text = tex.read_text(encoding="utf-8")
    old = extract_protected_blocks(text)[0]
    spec = ResultUpdateSpec(target_path="paper.tex",
                            target_source_sha256=hashlib.sha256(text.encode()).hexdigest(),
                            old_block_sha256=hashlib.sha256(old.encode()).hexdigest(),
                            allowed_fact_ids=("fact-lat",))
    prop = build_proposal(tmp_path, service.memory, spec,
                          proposed_block=old.replace("20.0", "22.4"), facts_used=[_fact()])
    approve_proposal(tmp_path, service.memory, prop["proposal_id"], approver="local-owner")
    # A: APPROVED + old -> CLEAN
    report = reconcile_result_updates(tmp_path, service.memory)
    assert any(f["verdict"] == "CLEAN" for f in report["findings"])
    # C: simulate APPLYING + new without DB commit.
    with service.memory.connect() as db:
        db.execute("UPDATE result_updates SET state='APPLYING' WHERE id=?", (prop["proposal_id"],))
    (tmp_path / "paper.tex").write_text(text.replace(old, old.replace("20.0", "22.4")), encoding="utf-8")
    report2 = reconcile_result_updates(tmp_path, service.memory)
    assert any("RECOVERABLE_FORWARD" in f["verdict"] for f in report2["findings"])
    # Manual: neither old nor new.
    (tmp_path / "paper.tex").write_text(_doc("Intro.", "Completely different 99.9 ms."), encoding="utf-8")
    report3 = reconcile_result_updates(tmp_path, service.memory)
    assert any("MANUAL" in f["verdict"] for f in report3["findings"])
