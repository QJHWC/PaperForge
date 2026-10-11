from pathlib import Path

import pytest

from paperforge.path_safety import UnsafePathError
from paperforge.publication_artifacts import (
    FactSummarySpec,
    FigureSpec,
    PublicationArtifactError,
    TableSpec,
    generate_facts,
    generate_figure,
    generate_table,
)
from tests.test_u23b_artifacts import _ledger_three_layers


def _generator(root, service, ledger, kind, artifact_id="safe"):
    if kind == "tables":
        return lambda: generate_table(root, service.memory, TableSpec(
            ledger_id=ledger["ledger_id"], table_id=artifact_id, columns=("latency_ms",)))
    if kind == "figures":
        return lambda: generate_figure(root, service.memory, FigureSpec(
            ledger_id=ledger["ledger_id"], figure_id=artifact_id,
            x_metric="latency_ms", y_metric="latency_ms"))
    return lambda: generate_facts(root, service.memory, FactSummarySpec(
        ledger_id=ledger["ledger_id"], fact_id=artifact_id,
        row_id=ledger["detail_rows"][0]["row_id"]))


@pytest.mark.parametrize("kind,member", [
    ("tables", "data.csv"), ("figures", "plot_data.json"), ("facts", "facts.json"),
])
@pytest.mark.parametrize("attack", ["temporary", "output", "manifest", "directory"])
def test_publication_writes_reject_links_before_any_output(tmp_path, kind, member, attack):
    root = tmp_path / "workspace"
    root.mkdir()
    service, ledger = _ledger_three_layers(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"must not change")
    folder = root / "artifacts" / kind / "safe"
    folder.parent.mkdir(parents=True, exist_ok=True)
    if attack == "directory":
        folder.symlink_to(outside, target_is_directory=True)
    else:
        folder.mkdir()
        name = {"temporary": f".{member}.tmp", "output": member,
                "manifest": "manifest.json"}[attack]
        (folder / name).symlink_to(sentinel)
    before = {p.name: p.read_bytes() for p in outside.iterdir() if p.is_file()}
    with pytest.raises((UnsafePathError, PublicationArtifactError)):
        _generator(root, service, ledger, kind)()
    assert {p.name: p.read_bytes() for p in outside.iterdir() if p.is_file()} == before
    if attack != "directory":
        assert len(list(folder.iterdir())) == 1


@pytest.mark.parametrize("kind", ["tables", "figures", "facts"])
@pytest.mark.parametrize("bad_id", ["../../../outside", "/absolute", "..\\outside"])
def test_publication_ids_cannot_escape_artifact_directory(tmp_path, kind, bad_id):
    root = tmp_path / "workspace"
    root.mkdir()
    service, ledger = _ledger_three_layers(root)
    with pytest.raises((UnsafePathError, PublicationArtifactError)):
        _generator(root, service, ledger, kind, bad_id)()
    assert not (tmp_path / "outside").exists()


@pytest.mark.parametrize("kind", ["tables", "figures", "facts"])
def test_safe_publication_retry_preserves_bytes_and_cleans_temporary_files(tmp_path: Path, kind):
    service, ledger = _ledger_three_layers(tmp_path)
    generate = _generator(tmp_path, service, ledger, kind)
    first = generate()
    folder = tmp_path / "artifacts" / kind / "safe"
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    assert generate() == first
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == before
    assert not list(folder.glob(".*"))
