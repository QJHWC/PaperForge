import hashlib
import json

import pytest
from test_u3b2_finalization import _pdf, _service

from paperforge.pdf_qa import HumanPageReview, PDFQASpec, run_pdf_qa, verify_pdf_qa


def _qa(tmp_path):
    service = _service(tmp_path)
    data = _pdf([[(12, "Fixture page")]])
    target = tmp_path / "fixture.pdf"
    target.write_bytes(data)
    spec = PDFQASpec(target_pdf_path="fixture.pdf", target_pdf_sha256=hashlib.sha256(data).hexdigest())
    auto = run_pdf_qa(tmp_path, service.memory, spec)
    return service, target, spec, auto


def test_pending_human_record_never_completes_coverage(tmp_path):
    service, _, spec, auto = _qa(tmp_path)
    pending = HumanPageReview(pdf_sha256=spec.target_pdf_sha256, page=1,
                              render_sha256=auto["render_manifest"][0]["image_hash"])
    result = run_pdf_qa(tmp_path, service.memory, spec, human_reviews=[pending])
    assert result["layout_qa_state"] == "needs_human"
    assert result["page_coverage_complete"] is False


@pytest.mark.parametrize("change", ["replace", "delete", "link", "spec"])
def test_pdf_qa_verifier_checks_current_target_and_bound_spec(tmp_path, change):
    service, target, _, auto = _qa(tmp_path)
    bundle = tmp_path / ".paperforge/pdf_qa" / auto["bundle_id"]
    frozen = (bundle / "qa.json").read_bytes()
    assert verify_pdf_qa(tmp_path, service.memory, auto["bundle_id"])["currently_valid"] is True
    if change == "replace":
        target.write_bytes(_pdf([[(12, "Changed page")]]))
    elif change == "delete":
        target.unlink()
    elif change == "link":
        duplicate = tmp_path / "duplicate.pdf"
        duplicate.write_bytes(target.read_bytes())
        target.unlink()
        target.symlink_to(duplicate)
    else:
        spec_path = bundle / "spec.json"
        spec = json.loads(spec_path.read_text())
        spec["target_pdf_path"] = "missing.pdf"
        spec_path.write_text(json.dumps(spec))
    result = verify_pdf_qa(tmp_path, service.memory, auto["bundle_id"])
    assert result["currently_valid"] is False
    assert result["target_pdf_hash_match"] is False
    assert result["render_currentness"] != "current"
    assert result["human_review_currentness"] != "current"
    assert result["eligible_as_layout_qa_input"] is False
    assert (bundle / "qa.json").read_bytes() == frozen
    if change != "spec":
        assert result["historical_integrity"] == "valid"
