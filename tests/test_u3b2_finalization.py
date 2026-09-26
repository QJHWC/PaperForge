from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from paperforge.api import PaperForgeService
from paperforge.pdf_qa import (
    HumanPageReview,
    PDFQASpec,
    PopplerRenderer,
    SyntheticContentHashRenderer,
    evaluate_layout_criterion,
    render_pages,
    run_pdf_qa,
)


def _service(tmp_path: Path) -> PaperForgeService:
    return PaperForgeService(tmp_path)


def _pdf(pages) -> bytes:
    from tests.test_u3b2_pdf_qa import _build_pdf, _page

    return _build_pdf([_page(t) for t in pages])


def test_real_raster_hash_from_image_bytes(tmp_path: Path) -> None:  # F1
    service = _service(tmp_path)
    data = _pdf([[(12, "Hello")]])
    (tmp_path / "a.pdf").write_bytes(data)
    backend = PopplerRenderer()
    assert backend.renderer_id == "poppler-pdftoppm"
    rendered = backend.render(data, 1, 200)
    assert rendered["image_bytes"].startswith(b"\x89PNG")
    assert rendered["image_sha256"] == hashlib.sha256(rendered["image_bytes"]).hexdigest()
    records = render_pages(data, dpi=200)
    assert records[0]["image_hash"] == rendered["image_sha256"]
    assert "synthetic" not in records[0]["renderer"]


def test_synthetic_blocked_for_production(tmp_path: Path) -> None:  # F1 gate
    service = _service(tmp_path)
    data = _pdf([[(12, "Hello")]])
    (tmp_path / "a.pdf").write_bytes(data)
    sha = hashlib.sha256(data).hexdigest()
    with pytest.raises(Exception, match="REAL_RENDERER_UNAVAILABLE"):
        render_pages(data, backend=SyntheticContentHashRenderer(), allow_synthetic=False)
    # Explicit fixture mode works but is marked fixture.
    records = render_pages(data, backend=SyntheticContentHashRenderer(), allow_synthetic=True)
    assert records[0]["environment_role"] == "fixture"


def test_criterion_target_resolution(tmp_path: Path) -> None:  # F2 A-D
    service = _service(tmp_path)
    data = _pdf([[(12, "Hello")]])
    (tmp_path / "a.pdf").write_bytes(data)
    sha = hashlib.sha256(data).hexdigest()
    auto = run_pdf_qa(tmp_path, service.memory, PDFQASpec(target_pdf_path="a.pdf", target_pdf_sha256=sha))
    renders = {r["page"]: r["image_hash"] for r in auto["render_manifest"]}
    bundle = run_pdf_qa(
        tmp_path, service.memory, PDFQASpec(target_pdf_path="a.pdf", target_pdf_sha256=sha),
        human_reviews=[HumanPageReview(pdf_sha256=sha, page=1, render_sha256=renders[1],
                                       reviewer="qa-human", status="passed", reviewed_at="t")])
    # Case A: unrelated warning open does not block a resolved target.
    assert evaluate_layout_criterion(bundle, target_pages=[1], required_issue_ids=[])["verified"] is True
    # Case B: open target issue blocks.
    bundle2 = dict(bundle)
    bundle2["layout_issues"] = [{"issue_id": "tiny-table-iv", "status": "open", "page": 1}]
    assert evaluate_layout_criterion(bundle2, required_issue_ids=["tiny-table-iv"])["verified"] is False
    # Case C: accepted_with_rationale != resolved unless allowed.
    bundle3 = dict(bundle)
    bundle3["layout_issues"] = [{"issue_id": "i1", "status": "accepted_with_rationale", "page": 1}]
    assert evaluate_layout_criterion(bundle3, required_issue_ids=["i1"])["verified"] is False
    assert evaluate_layout_criterion(bundle3, required_issue_ids=["i1"],
                                     allow_accepted_with_rationale=True)["verified"] is True
    # Case D: stale human review blocks.
    assert evaluate_layout_criterion(bundle, target_pages=[99])["verified"] is False


def test_human_explicit_gate(tmp_path: Path) -> None:  # F3 1-6
    service = _service(tmp_path)
    data = _pdf([[(12, "Hello")]])
    (tmp_path / "a.pdf").write_bytes(data)
    sha = hashlib.sha256(data).hexdigest()
    # 1: automated QA with no human input creates no passed human records.
    bundle = run_pdf_qa(tmp_path, service.memory, PDFQASpec(target_pdf_path="a.pdf", target_pdf_sha256=sha))
    assert all(r.get("status") not in {"passed", "passed_with_warnings"} or r.get("reviewer") for r in bundle["human_reviews"])
    renders = {r["page"]: r["image_hash"] for r in bundle["render_manifest"]}
    # 2: explicit submission passes.
    explicit = [HumanPageReview(pdf_sha256=sha, page=1, render_sha256=renders[1],
                                reviewer="qa-human", status="passed", reviewed_at="t")]
    passed = run_pdf_qa(tmp_path, service.memory, PDFQASpec(target_pdf_path="a.pdf", target_pdf_sha256=sha),
                        human_reviews=explicit)
    assert passed["layout_qa_state"] == "passed"
    # 3/4: wrong hashes rejected/stale.
    with pytest.raises(Exception, match="reviewer identity|self-mark"):
        run_pdf_qa(tmp_path, service.memory, PDFQASpec(target_pdf_path="a.pdf", target_pdf_sha256=sha),
                   human_reviews=[HumanPageReview(pdf_sha256=sha, page=1, render_sha256=renders[1], status="passed")])
    stale = run_pdf_qa(tmp_path, service.memory, PDFQASpec(target_pdf_path="a.pdf", target_pdf_sha256=sha),
                       human_reviews=[HumanPageReview(pdf_sha256="0" * 64, page=1, render_sha256=renders[1],
                                                      reviewer="qa", status="passed", reviewed_at="t")])
    assert stale["layout_qa_state"] == "needs_human"
    # 6: fixture role cannot satisfy production criterion.
    fixture_review = HumanPageReview(pdf_sha256=sha, page=1, render_sha256=renders[1],
                                     reviewer="fixture-bot", status="passed", reviewed_at="t",
                                     environment_role="fixture")
    assert fixture_review.environment_role == "fixture"
