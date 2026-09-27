from __future__ import annotations

import json

import pytest

from paperforge.api import PaperForgeService
from paperforge.artifacts import ArtifactIntegrityError, ArtifactStore
from paperforge.immutable_artifacts import verify_registered_bundle
from test_u4b_finalization import setup_target


def bundle_factory(root, kind):
    service = PaperForgeService(root)
    if kind == "ledger":
        from paperforge.result_ledger import ResultLedgerQuery, freeze_ledger, verify_ledger

        eid = service.memory.add_evidence(evidence_type="TEST", excerpt="1 ms", metadata={
            "measurement_kind": "measured", "acquisition_origin": "paperforge_run", "environment_role": "scientific",
            "metric_name": "latency_ms", "value_str": "1", "unit": "ms", "run_identity": {"schema": "paperforge.scientific-run-identity/v1"}})
        query = ResultLedgerQuery(purpose="scientific", metric_names=["latency_ms"], evidence_ids=(eid,))
        return service, lambda: freeze_ledger(root, service.memory, query), "ledger_id", "ledgers", "ledger.json", lambda i: verify_ledger(root, service.memory, i)["eligible_as_release_input"]
    if kind == "review":
        from paperforge.scientific_review import ScientificClaim, freeze_review, review_claim, verify_scientific_review

        claim = ScientificClaim("c", "descriptive", "descriptive_metric", fact_ids=("f",))
        return service, lambda: freeze_review(root, service.memory, claims=[claim], results=review_claim(claim, facts={"f": {}})), "review_id", "reviews", "review.json", lambda i: verify_scientific_review(root, service.memory, i)["currently_valid_as_review_input"]
    if kind == "matrix":
        from paperforge.reviewer_matrix import freeze_review_matrix, verify_review_matrix

        return service, lambda: freeze_review_matrix(root, service.memory, comments=[], actions=[], criteria=[]), "matrix_id", "review_matrix", "matrix.json", lambda i: verify_review_matrix(root, service.memory, i)["currently_valid"]
    if kind == "pdf":
        from paperforge.pdf_qa import PDFQASpec, SyntheticContentHashRenderer, run_pdf_qa, verify_pdf_qa
        from test_u3b2_pdf_qa import _build_pdf

        (root / "fixture.pdf").write_bytes(_build_pdf([{"texts": [(12, "Fixture")]}]))
        spec = PDFQASpec(target_pdf_path="fixture.pdf")
        return service, lambda: run_pdf_qa(root, service.memory, spec, renderer_backend=SyntheticContentHashRenderer(), allow_synthetic=True), "bundle_id", "pdf_qa", "qa.json", lambda i: verify_pdf_qa(root, service.memory, i)["currently_valid"]
    from paperforge.release_eligibility import assessment_current, evaluate_release, freeze_assessment

    service, target, policy = setup_target(root)
    report = evaluate_release(root, service.memory, target, policy)
    return service, lambda: freeze_assessment(root, service.memory, report), "report_id", "release", "report.json", lambda i: assessment_current(root, service.memory, i)["currently_valid"]


@pytest.mark.parametrize("kind", ["ledger", "review", "matrix", "pdf", "release"])
def test_registration_interrupted_retry_preserves_bytes(tmp_path, monkeypatch, kind):
    service, freeze, identity_key, folder, filename, current = bundle_factory(tmp_path, kind)
    original = ArtifactStore.register
    calls = []

    def interrupted(self, path, **kwargs):
        calls.append(str(path))
        if len(calls) == 2:
            raise OSError("injected interruption between registry records")
        return original(self, path, **kwargs)

    monkeypatch.setattr(ArtifactStore, "register", interrupted)
    with pytest.raises(OSError, match="interruption"):
        freeze()
    path = next((tmp_path / ".paperforge" / folder).glob(f"*/{filename}"))
    before = path.read_bytes()
    identity = json.loads(before)[identity_key]
    assert not current(identity)
    monkeypatch.setattr(ArtifactStore, "register", original)
    payload = freeze()
    assert payload[identity_key] == identity
    assert current(identity)
    assert path.read_bytes() == before
    with service.memory.connect() as db:
        baseline = list(db.iterdump())
    for _ in range(20):
        assert freeze()[identity_key] == identity
        assert current(identity)
    with service.memory.connect() as db:
        assert list(db.iterdump()) == baseline
        assert db.execute("SELECT count(*) FROM artifacts WHERE path LIKE ?", (f".paperforge/{folder}/{identity}/%",)).fetchone()[0] == 2
    assert not list(path.parent.glob("*.tmp"))
    assert not list(path.parent.glob("*.partial"))
    assert not list(path.parent.glob(".*.json.*"))


@pytest.mark.parametrize("kind", ["ledger", "review", "matrix", "pdf", "release"])
@pytest.mark.parametrize("damage", ["missing", "bytes", "registry", "kind"])
def test_registry_split_brain_never_current(tmp_path, kind, damage):
    service, freeze, identity_key, folder, filename, current = bundle_factory(tmp_path, kind)
    payload = freeze()
    identity = payload[identity_key]
    rel = f".paperforge/{folder}/{identity}/manifest.json"
    path = tmp_path / rel
    if damage == "missing":
        path.unlink()
    elif damage == "bytes":
        path.write_bytes(b'{"unexpected": true}')
    elif damage == "registry":
        with service.memory.connect() as db:
            db.execute("UPDATE artifacts SET sha256=? WHERE path=?", ("0" * 64, rel))
    else:
        with service.memory.connect() as db:
            db.execute("UPDATE artifacts SET kind='review' WHERE path=?", (rel,))
    assert not current(identity)
    audit = verify_registered_bundle(tmp_path, service.memory, {rel: "manifest"})
    assert not audit["registered_complete"]
    if damage == "missing":
        freeze()
        assert current(identity)
    else:
        with pytest.raises(ArtifactIntegrityError):
            freeze()
        assert not current(identity)


@pytest.mark.parametrize("kind", ["ledger", "review", "matrix", "pdf", "release"])
def test_partial_json_is_not_authority_or_silently_overwritten(tmp_path, kind):
    _, freeze, _, folder, filename, _ = bundle_factory(tmp_path, kind)
    freeze()
    path = next((tmp_path / ".paperforge" / folder).glob(f"*/{filename}"))
    path.write_bytes(b'{"half":')
    with pytest.raises((ArtifactIntegrityError, ValueError)):
        freeze()
    assert path.read_bytes() == b'{"half":'
