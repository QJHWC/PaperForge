import json

import pytest
from test_u4d_artifact_write_safety import _generator
from test_u23b_artifacts import _ledger_three_layers

from paperforge.publication_artifacts import PublicationArtifactError


@pytest.mark.parametrize("kind", ["tables", "figures", "facts"])
@pytest.mark.parametrize("damage", ["ledger", "registry", "source"])
def test_generators_refuse_damaged_or_stale_ledger(tmp_path, kind, damage):
    service, ledger = _ledger_three_layers(tmp_path)
    path = tmp_path / ".paperforge" / "ledgers" / ledger["ledger_id"] / "ledger.json"
    if damage == "ledger":
        payload = json.loads(path.read_text())
        payload["detail_rows"][0]["value_str"] = "999"
        path.write_text(json.dumps(payload))
    elif damage == "registry":
        with service.memory.connect() as db:
            db.execute("DELETE FROM artifacts WHERE path=?", (path.relative_to(tmp_path).as_posix(),))
    else:
        with service.memory.connect() as db:
            db.execute("DELETE FROM evidence WHERE id=?", (ledger["snapshots"][0]["evidence_id"],))
    with pytest.raises(PublicationArtifactError, match="ledger"):
        _generator(tmp_path, service, ledger, kind)()
    assert not (tmp_path / "artifacts" / kind).exists()
