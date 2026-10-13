import json

from test_u4d_import_ledger import imported_metric

from paperforge.external_import import approval_valid, reconcile_imports


def test_same_byte_symlink_cannot_keep_import_approval_or_clean_recovery(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    service, record, _, raw = imported_metric(root)
    base = root / ".paperforge/imports" / record.import_id
    manifest = json.loads((base / "manifest.json").read_bytes())
    source = base / manifest["sources"][0]["path"]
    outside = tmp_path / "outside.json"
    outside.write_bytes(raw)
    source.unlink()
    source.symlink_to(outside)
    with service.memory.connect() as db:
        before = list(db.iterdump())
    valid, _ = approval_valid(root, service.memory, record.import_id)
    assert valid is False
    report = reconcile_imports(root, service.memory, repair=True)
    assert report["recovery_outcome"] == "MANUAL_INTERVENTION_REQUIRED"
    assert report["repaired"] is False
    assert source.is_symlink()
    assert outside.read_bytes() == raw
    with service.memory.connect() as db:
        assert list(db.iterdump()) == before
