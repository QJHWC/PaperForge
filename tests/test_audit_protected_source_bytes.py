import pytest
from test_u1_patch import _document

from paperforge.api import PaperForgeService
from paperforge.protected_blocks import (
    ProtectedBlockViolation,
    ProtectedEditTransaction,
    extract_protected_blocks,
)
from paperforge.writing import WritingEngine, WritingSafetyError


def test_full_writing_cannot_normalize_protected_block_bytes(tmp_path):
    source = _document("Original prose.").replace("\n", "\r\n").encode()
    path = tmp_path / "paper.tex"
    path.write_bytes(source)
    service = PaperForgeService(tmp_path)
    engine = WritingEngine(tmp_path, memory=service.memory,
                           request_text=lambda *a, **k: _document("Revised prose."))
    with pytest.raises(WritingSafetyError, match="protected"):
        engine.write(run_id="test", payload={"main_tex": "paper.tex", "instructions": "Refine prose."},
                     model="bailu-turing")
    assert path.read_bytes() == source


def test_protected_edit_transaction_detects_newline_mutation_and_restores_bytes(tmp_path):
    source = _document("Original prose.").replace("\n", "\r\n").encode()
    path = tmp_path / "paper.tex"
    path.write_bytes(source)
    transaction = ProtectedEditTransaction(path, require_markers=True)
    path.write_bytes(source.replace(b"\r\n", b"\n"))
    with pytest.raises(ProtectedBlockViolation, match="protected"):
        transaction.verify()
    assert path.read_bytes() == source


def test_full_writing_accepts_byte_preserving_crlf_output(tmp_path):
    source = _document("Original prose.").replace("\n", "\r\n").encode()
    path = tmp_path / "paper.tex"
    path.write_bytes(source)
    service = PaperForgeService(tmp_path)
    engine = WritingEngine(tmp_path, memory=service.memory,
                           request_text=lambda *a, **k: _document("Revised prose.").replace("\n", "\r\n"))
    engine.write(run_id="test", payload={"main_tex": "paper.tex", "instructions": "Refine prose."},
                 model="bailu-turing")
    assert extract_protected_blocks(path.read_bytes().decode()) == extract_protected_blocks(source.decode())
    assert b"Revised prose." in path.read_bytes()
