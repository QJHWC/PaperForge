import pytest

from paperforge.external_import import validate_import
from paperforge.path_safety import UnsafePathError
from paperforge.result_update import build_proposal
from tests.test_u22_import_transaction import _stage
from tests.test_u24a_finalization import _fact, _spec_with_table


@pytest.mark.parametrize("member", ["spec.json", "proposal.json", "validation.json"])
@pytest.mark.parametrize("attack", ["temporary", "output"])
def test_proposal_write_preflights_all_members(tmp_path, member, attack):
    root = tmp_path / "workspace"
    root.mkdir()
    service, spec, old, _ = _spec_with_table(root, "Old result 20.0 ms.")
    new = old.replace("20.0", "22.4")
    proposal = build_proposal(root, service.memory, spec, proposed_block=new, facts_used=[_fact()])
    base = root / ".paperforge/result_updates" / proposal["proposal_id"]
    target = base / (f".{member}.tmp" if attack == "temporary" else member)
    if target.exists():
        target.unlink()
    sentinel = tmp_path / "outside"
    sentinel.write_bytes(b"preserve")
    target.symlink_to(sentinel)
    before = {p.name: p.read_bytes() for p in base.iterdir()}
    with service.memory.connect() as db:
        before_db = list(db.iterdump())
    with pytest.raises(UnsafePathError):
        build_proposal(root, service.memory, spec, proposed_block=new, facts_used=[_fact()])
    assert sentinel.read_bytes() == b"preserve"
    assert {p.name: p.read_bytes() for p in base.iterdir()} == before
    with service.memory.connect() as db:
        assert list(db.iterdump()) == before_db


@pytest.mark.parametrize("attack", ["temporary", "output"])
def test_import_validation_does_not_follow_writable_symlinks(tmp_path, attack):
    root = tmp_path / "workspace"
    root.mkdir()
    service, record = _stage(root)
    base = root / ".paperforge/imports" / record.import_id
    target = base / (f".validation.json.{record.import_id}.tmp"
                     if attack == "temporary" else "validation.json")
    if target.exists():
        target.unlink()
    sentinel = tmp_path / "outside"
    sentinel.write_bytes(b"preserve")
    target.symlink_to(sentinel)
    with pytest.raises(UnsafePathError):
        validate_import(root, service.memory, record.import_id)
    assert sentinel.read_bytes() == b"preserve"
