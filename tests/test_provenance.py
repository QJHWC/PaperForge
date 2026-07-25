from pathlib import Path

import pytest

from paperforge.provenance import ProvenanceError, capture_source_snapshot


def test_source_snapshot_records_content_and_git_blob_hashes(tmp_path: Path) -> None:
    (tmp_path / "source.py").write_text("value = 1\n", encoding="utf-8")
    snapshot = capture_source_snapshot(
        tmp_path,
        uri="https://example.invalid/source",
        commit="abc",
    )
    assert snapshot["file_count"] == 1
    assert len(snapshot["tree_sha256"]) == 64
    assert len(snapshot["files"][0]["git_blob_sha1"]) == 40

    outside = tmp_path.parent / "outside"
    outside.write_text("outside", encoding="utf-8")
    (tmp_path / "link").symlink_to(outside)
    with pytest.raises(ProvenanceError):
        capture_source_snapshot(tmp_path, uri="x", commit="abc")
