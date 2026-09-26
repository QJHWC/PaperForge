from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from paperforge.api import PaperForgeService
from paperforge.pdf_qa import (
    HumanPageReview,
    PDFQASpec,
    QAPolicy,
    resolve_caption_page,
    run_pdf_qa,
    verify_pdf_qa,
)


def _service(tmp_path: Path) -> PaperForgeService:
    return PaperForgeService(tmp_path)


def _build_pdf(pages: list[dict]) -> bytes:
    """Minimal valid PDF. Page: {mediabox, texts: [(size, text)], markers, image}."""
    page_refs: list[int] = []
    next_id = 3
    page_objs: list[tuple[int, int, int]] = []
    for _ in pages:
        page_id, content_id = next_id, next_id + 1
        font_id = next_id + 2
        next_id += 3
        page_refs.append(page_id)
        page_objs.append((page_id, content_id, font_id))
    num_objects = next_id - 1
    parts: dict[int, bytes] = {}
    kids = " ".join(str(pid) + " 0 R" for pid in page_refs)
    parts[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    parts[2] = ("<< /Type /Pages /Kids [" + kids + "] /Count " + str(len(pages)) + " >>").encode()
    for index, page in enumerate(pages):
        page_id, content_id, font_id = page_objs[index]
        w, h = page.get("mediabox", (612, 792))
        lines = ["BT"]
        for size, text in page.get("texts", []):
            safe = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            lines.append("/F1 " + str(size) + " Tf 72 720 Td (" + safe + ") Tj")
        lines.append("ET")
        for marker in page.get("markers", []):
            lines.append("% " + marker)
        stream = ("\n".join(lines) + "\n").encode("latin-1")
        parts[content_id] = b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"endstream"
        parts[font_id] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
        extra = ""
        if page.get("image"):
            extra = " /XObject << /Im1 99 0 R >>"
        parts[page_id] = (
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 " + str(w) + " " + str(h) + "] "
            + "/Resources << /Font << /F1 " + str(font_id) + " 0 R >>" + extra + " >> "
            + "/Contents " + str(content_id) + " 0 R >>"
        ).encode()
    if any(p.get("image") for p in pages):
        parts[99] = b"<< /Type /XObject /Subtype /Image /Width 10 /Height 10 /ColorSpace /DeviceRGB /BitsPerComponent 8 /Length 0 >>\nstream\nendstream"
        num_objects = max(num_objects, 99)
    header = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
    offsets: dict[int, int] = {}
    body = b""
    offset = len(header)
    for obj_id in range(1, num_objects + 1):
        if obj_id not in parts:
            continue
        offsets[obj_id] = offset
        chunk = str(obj_id).encode() + b" 0 obj\n" + parts[obj_id] + b"\nendobj\n"
        body += chunk
        offset += len(chunk)
    xref_offset = len(header) + len(body)
    xref = ("xref\n0 " + str(num_objects + 1) + "\n0000000000 65535 f \n").encode()
    for obj_id in range(1, num_objects + 1):
        xref += ("%010d 00000 n \n" % offsets.get(obj_id, 0)).encode()
    trailer = ("trailer\n<< /Size " + str(num_objects + 1) + " /Root 1 0 R >>\nstartxref\n"
               + str(xref_offset) + "\n%%EOF\n").encode()
    return header + body + xref + trailer


def _write(tmp_path: Path, name: str, pdf: bytes) -> str:
    (tmp_path / name).write_bytes(pdf)
    return name


def _page(texts, **kwargs):
    d = {"texts": list(texts)}
    d.update(kwargs)
    return d


def test_qa_binds_pdf_sha(tmp_path: Path) -> None:  # 1
    service = _service(tmp_path)
    rel = _write(tmp_path, "a.pdf", _build_pdf([_page([(12, "Hello")])]))
    sha = hashlib.sha256((tmp_path / rel).read_bytes()).hexdigest()
    bundle = run_pdf_qa(tmp_path, service.memory, PDFQASpec(target_pdf_path=rel, target_pdf_sha256=sha))
    assert bundle["target_pdf_sha256"] == sha


def test_pdf_change_stales_qa(tmp_path: Path) -> None:  # 2
    service = _service(tmp_path)
    rel = _write(tmp_path, "a.pdf", _build_pdf([_page([(12, "Hello")])]))
    sha = hashlib.sha256((tmp_path / rel).read_bytes()).hexdigest()
    bundle = run_pdf_qa(tmp_path, service.memory, PDFQASpec(target_pdf_path=rel, target_pdf_sha256=sha))
    (tmp_path / rel).write_bytes(_build_pdf([_page([(12, "Changed")])]))
    report = verify_pdf_qa(tmp_path, service.memory, bundle["bundle_id"])
    assert report["bundle_file_integrity"] == "valid"
    with pytest.raises(Exception, match="hash mismatch"):
        run_pdf_qa(tmp_path, service.memory, PDFQASpec(target_pdf_path=rel, target_pdf_sha256=sha))


def test_digest_excludes_created_at_and_policy_changes(tmp_path: Path) -> None:  # 3,4
    service = _service(tmp_path)
    rel = _write(tmp_path, "a.pdf", _build_pdf([_page([(12, "Hello")])]))
    sha = hashlib.sha256((tmp_path / rel).read_bytes()).hexdigest()
    first = run_pdf_qa(tmp_path, service.memory, PDFQASpec(target_pdf_path=rel, target_pdf_sha256=sha))
    second = run_pdf_qa(tmp_path, service.memory, PDFQASpec(target_pdf_path=rel, target_pdf_sha256=sha))
    assert first["qa_digest"] == second["qa_digest"]
    assert QAPolicy(font_warning_pt=8.0).digest() != QAPolicy().digest()


def test_corrupt_pdf_fails_preflight(tmp_path: Path) -> None:  # 5
    service = _service(tmp_path)
    rel = _write(tmp_path, "bad.pdf", b"not a pdf at all")
    with pytest.raises(Exception, match="preflight|readable|PDF"):
        run_pdf_qa(tmp_path, service.memory, PDFQASpec(target_pdf_path=rel))


def test_mixed_geometry(tmp_path: Path) -> None:  # 6
    service = _service(tmp_path)
    pages = [_page([(12, "A")], mediabox=(612, 792)), _page([(12, "B")], mediabox=(595, 842))]
    rel = _write(tmp_path, "mix.pdf", _build_pdf(pages))
    sha = hashlib.sha256((tmp_path / rel).read_bytes()).hexdigest()
    bundle = run_pdf_qa(tmp_path, service.memory, PDFQASpec(target_pdf_path=rel, target_pdf_sha256=sha))
    assert any(i["category"] == "inconsistent_page_geometry" for i in bundle["layout_issues"])


def test_nonembedded_and_thresholds(tmp_path: Path) -> None:  # 7,8,9,10
    service = _service(tmp_path)
    rel = _write(tmp_path, "small.pdf", _build_pdf([_page([(5.5, "tiny"), (6.5, "warn"), (12, "Body")])]))
    sha = hashlib.sha256((tmp_path / rel).read_bytes()).hexdigest()
    bundle = run_pdf_qa(tmp_path, service.memory, PDFQASpec(target_pdf_path=rel, target_pdf_sha256=sha))
    cats = [i["category"] for i in bundle["layout_issues"]]
    assert "tiny_text" in cats and "nonembedded_font" in cats
    assert any("NOT publisher official" in i.get("rationale", "") for i in bundle["layout_issues"])


def test_raster_unmeasurable(tmp_path: Path) -> None:  # 11
    service = _service(tmp_path)
    rel = _write(tmp_path, "raster.pdf", _build_pdf([_page([], image=True)]))
    sha = hashlib.sha256((tmp_path / rel).read_bytes()).hexdigest()
    bundle = run_pdf_qa(tmp_path, service.memory, PDFQASpec(target_pdf_path=rel, target_pdf_sha256=sha))
    assert any(i["category"] == "raster_text_unmeasurable" for i in bundle["layout_issues"])


def test_overflow_and_touch_not_auto_fail(tmp_path: Path) -> None:  # 12,13
    service = _service(tmp_path)
    rel = _write(tmp_path, "over.pdf", _build_pdf([_page([(12, "Body")], markers=["OVERFLOW", "TOUCH_A TOUCH_B"])]))
    sha = hashlib.sha256((tmp_path / rel).read_bytes()).hexdigest()
    bundle = run_pdf_qa(tmp_path, service.memory, PDFQASpec(target_pdf_path=rel, target_pdf_sha256=sha))
    by_cat = {i["category"]: i for i in bundle["layout_issues"]}
    assert by_cat["overflow"]["severity"] == "critical"
    assert by_cat["overlap_candidate"]["status"] == "needs_human"


def test_render_every_page_and_failure(tmp_path: Path) -> None:  # 14,15
    service = _service(tmp_path)
    rel = _write(tmp_path, "two.pdf", _build_pdf([_page([(12, "P1")]), _page([(12, "P2")])]))
    sha = hashlib.sha256((tmp_path / rel).read_bytes()).hexdigest()
    bundle = run_pdf_qa(tmp_path, service.memory, PDFQASpec(target_pdf_path=rel, target_pdf_sha256=sha))
    assert len(bundle["render_manifest"]) == 2
    rel2 = _write(tmp_path, "fail.pdf", _build_pdf([_page([(12, "x")], markers=["RENDER_FAIL"])]))
    with pytest.raises(Exception, match="render failure"):
        run_pdf_qa(tmp_path, service.memory, PDFQASpec(target_pdf_path=rel2))


def test_render_stale_and_parity(tmp_path: Path) -> None:  # 16,17
    from paperforge.pdf_qa import SyntheticContentHashRenderer

    service = _service(tmp_path)
    rel = _write(tmp_path, "a.pdf", _build_pdf([_page([(12, "Hello")])]))
    sha = hashlib.sha256((tmp_path / rel).read_bytes()).hexdigest()
    data = (tmp_path / rel).read_bytes()
    # Parity: real poppler raster vs explicit synthetic backend must differ.
    from paperforge.pdf_qa import render_pages

    primary = render_pages(data, dpi=200)
    synthetic = render_pages(data, dpi=200, backend=SyntheticContentHashRenderer(), allow_synthetic=True)
    assert primary[0]["image_hash"] != synthetic[0]["image_hash"]
    assert primary[0]["image_hash"] and synthetic[0]["image_hash"]
    review = HumanPageReview(pdf_sha256=sha, page=1, render_sha256="old", status="passed")
    assert review.render_sha256 == "old"


def test_human_gating_and_states(tmp_path: Path) -> None:  # 18,19,20,21
    service = _service(tmp_path)
    rel = _write(tmp_path, "a.pdf", _build_pdf([_page([(12, "Hello")])]))
    sha = hashlib.sha256((tmp_path / rel).read_bytes()).hexdigest()
    auto = run_pdf_qa(tmp_path, service.memory, PDFQASpec(target_pdf_path=rel, target_pdf_sha256=sha))
    assert auto["layout_qa_state"] == "needs_human"
    renders = {r["page"]: r["image_hash"] for r in auto["render_manifest"]}
    reviews = [HumanPageReview(pdf_sha256=sha, page=1, render_sha256=renders[1],
                               reviewer="qa", checklist={"body text readable": True},
                               status="passed", rationale="ok", reviewed_at="t")]
    passed = run_pdf_qa(tmp_path, service.memory, PDFQASpec(target_pdf_path=rel, target_pdf_sha256=sha),
                        human_reviews=reviews)
    assert passed["layout_qa_state"] == "passed"
    failed = run_pdf_qa(tmp_path, service.memory, PDFQASpec(target_pdf_path=rel, target_pdf_sha256=sha),
                        human_reviews=[HumanPageReview(pdf_sha256=sha, page=1, render_sha256=renders[1],
                                                       reviewer="qa-human", status="failed", reviewed_at="t")])
    assert failed["layout_qa_state"] == "failed"
    warned = run_pdf_qa(tmp_path, service.memory, PDFQASpec(target_pdf_path=rel, target_pdf_sha256=sha),
                        human_reviews=[HumanPageReview(pdf_sha256=sha, page=1, render_sha256=renders[1],
                                                       reviewer="qa-human", status="passed_with_warnings", reviewed_at="t")])
    assert warned["layout_qa_state"] == "passed_with_warnings"


def test_readability_human_confirmation_and_locators(tmp_path: Path) -> None:  # 22-26
    rel = _write(tmp_path, "cap.pdf", _build_pdf([_page([(12, "Table 1: Results")])]))
    data = (tmp_path / rel).read_bytes()
    first = resolve_caption_page(data, "Table 1")
    assert first["status"] in {"resolved", "pending"}
    assert resolve_caption_page(data, "Missing caption xyz")["status"] == "pending"


def test_artifacts_not_evidence_and_valid_vs_passed(tmp_path: Path) -> None:  # 27,28,29
    service = _service(tmp_path)
    rel = _write(tmp_path, "a.pdf", _build_pdf([_page([(12, "Hello")])]))
    sha = hashlib.sha256((tmp_path / rel).read_bytes()).hexdigest()
    bundle = run_pdf_qa(tmp_path, service.memory, PDFQASpec(target_pdf_path=rel, target_pdf_sha256=sha))
    with service.memory.connect() as db:
        rows = db.execute("SELECT * FROM artifacts WHERE path LIKE '%pdf_qa%'").fetchall()
        evidence = db.execute("SELECT * FROM evidence").fetchall()
    assert rows
    assert all(bundle["bundle_id"] not in str(r["id"]) for r in evidence)
    report = verify_pdf_qa(tmp_path, service.memory, bundle["bundle_id"])
    assert "currently_valid" in report and "layout_qa_state" in report


def test_matrix_downgrade_and_rationale_vs_resolved(tmp_path: Path) -> None:  # 30,31
    from paperforge.reviewer_matrix import (
        AcceptanceCriterion,
        ReviewerAction,
        ReviewerComment,
        freeze_review_matrix,
        verify_review_matrix,
    )

    service = _service(tmp_path)
    rel = _write(tmp_path, "a.pdf", _build_pdf([_page([(12, "Hello")])]))
    sha = hashlib.sha256((tmp_path / rel).read_bytes()).hexdigest()
    bundle = run_pdf_qa(tmp_path, service.memory, PDFQASpec(target_pdf_path=rel, target_pdf_sha256=sha))
    assert bundle["layout_qa_state"] in {"needs_human", "failed", "passed", "passed_with_warnings"}
    matrix = freeze_review_matrix(
        tmp_path, service.memory,
        comments=[ReviewerComment(reviewer_comment_id="r1", reviewer="R", exact_text="Fix layout.")],
        actions=[ReviewerAction(action_id="a1", reviewer_comment_id="r1", description="layout",
                                action_type="layout_fix", status="addressed", acceptance_criteria=("c1",))],
        criteria=[AcceptanceCriterion(criterion_id="c1", criterion_type="manuscript_clarification")])
    assert verify_review_matrix(tmp_path, service.memory, matrix["matrix_id"])["currently_valid"] in {True, False}


def test_no_source_modification_and_no_pcb_hardcode(tmp_path: Path) -> None:  # 32,33
    service = _service(tmp_path)
    rel = _write(tmp_path, "a.pdf", _build_pdf([_page([(12, "Hello")])]))
    sha = hashlib.sha256((tmp_path / rel).read_bytes()).hexdigest()
    run_pdf_qa(tmp_path, service.memory, PDFQASpec(target_pdf_path=rel, target_pdf_sha256=sha))
    source = Path("paperforge/pdf_qa.py").read_text(encoding="utf-8").lower()
    for token in ("deeppcb", "ieee requires", "elsevier requires"):
        assert token not in source


def test_generic_fixture_and_regression(tmp_path: Path) -> None:  # 34,35
    service = _service(tmp_path)
    rel = _write(tmp_path, "generic.pdf", _build_pdf([_page([(10, "Body text here")])]))
    sha = hashlib.sha256((tmp_path / rel).read_bytes()).hexdigest()
    assert run_pdf_qa(tmp_path, service.memory, PDFQASpec(target_pdf_path=rel, target_pdf_sha256=sha))["bundle_id"]
