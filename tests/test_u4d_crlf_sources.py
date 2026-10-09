"""Source hashes and protected blocks bind bytes, not normalized newlines."""

import hashlib

import pytest
from test_u1_patch import _document

from paperforge.api import PaperForgeService
from paperforge.protected_blocks import extract_protected_blocks
from paperforge.result_update import (
    ResultUpdateSpec,
    apply_proposal,
    approve_proposal,
    build_proposal,
    reconcile_result_updates,
)
from paperforge.writing_patch import apply_patch


@pytest.fixture(autouse=True)
def windows_default_text_newlines(monkeypatch):
    from paperforge import path_safety

    original = path_safety.tempfile.NamedTemporaryFile

    def windows_temporary_file(*args, **kwargs):
        if "b" not in kwargs.get("mode", "w+b") and kwargs.get("newline") is None:
            kwargs["newline"] = "\r\n"
        return original(*args, **kwargs)

    monkeypatch.setattr(path_safety.tempfile, "NamedTemporaryFile", windows_temporary_file)


def test_prose_patch_preserves_crlf_source_bytes(tmp_path):
    path = tmp_path / "paper.tex"
    original = _document("Original prose.").replace("\n", "\r\n").encode()
    path.write_bytes(original)
    service = PaperForgeService(tmp_path)
    apply_patch(tmp_path, memory=service.memory,
                request_text=lambda *a, **k: "Revised prose.", run_id="crlf-test",
                target="paper.tex", source_sha256=hashlib.sha256(original).hexdigest(),
                anchor="Original prose.")
    assert path.read_bytes() == original.replace(b"Original prose.", b"Revised prose.")


def test_prose_patch_rollback_preserves_crlf_bytes(tmp_path, monkeypatch):
    from paperforge.artifacts import ArtifactStore

    path = tmp_path / "paper.tex"
    original = _document("Original prose.").replace("\n", "\r\n").encode()
    path.write_bytes(original)
    service = PaperForgeService(tmp_path)

    def fail_registration(*args, **kwargs):
        raise RuntimeError("test registration failure")

    monkeypatch.setattr(ArtifactStore, "register", fail_registration)
    with pytest.raises(RuntimeError, match="test registration failure"):
        apply_patch(tmp_path, memory=service.memory,
                    request_text=lambda *a, **k: "Revised prose.", run_id="crlf-test",
                    target="paper.tex", source_sha256=hashlib.sha256(original).hexdigest(),
                    anchor="Original prose.")
    assert path.read_bytes() == original


def test_result_update_binds_crlf_bytes_through_apply_and_reconcile(tmp_path):
    path = tmp_path / "paper.tex"
    original = _document("Original prose.", "Old result 20.0 ms.").replace("\n", "\r\n").encode()
    path.write_bytes(original)
    old = extract_protected_blocks(original.decode())[0]
    service = PaperForgeService(tmp_path)
    spec = ResultUpdateSpec(target_path="paper.tex",
                            target_source_sha256=hashlib.sha256(original).hexdigest(),
                            old_block_sha256=hashlib.sha256(old.encode()).hexdigest(),
                            allowed_fact_ids=("test-latency",))
    proposal = build_proposal(tmp_path, service.memory, spec,
                              proposed_block=old.replace("20.0", "22.4"),
                              facts_used=[{"fact_id": "test-latency", "value": "22.4", "unit": "ms"}])
    approve_proposal(tmp_path, service.memory, proposal["proposal_id"], approver="TEST_ONLY")
    result = apply_proposal(tmp_path, service.memory, proposal["proposal_id"])
    expected = original.replace(b"20.0", b"22.4")
    assert path.read_bytes() == expected
    assert result["new_source_sha256"] == hashlib.sha256(expected).hexdigest()
    report = reconcile_result_updates(tmp_path, service.memory)
    assert all(f["verdict"] == "CLEAN" for f in report["findings"])
