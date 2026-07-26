from __future__ import annotations

import struct
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from paperforge.plugins import VisualizationSpec
from paperforge.publication import visual_checks
from paperforge.publication.visual_checks import (
    _pdftohtml_layout_boxes,
    inspect_page_structure,
    inspect_rendered_page,
    inspect_rendered_pages,
)
from paperforge.release import ReleaseVerificationError, write_page_inspection
from paperforge.visualization import VisualizationExporter, build_chart_model


def _write_grayscale_png(
    path: Path,
    *,
    width: int = 120,
    height: int = 120,
    ink_border: bool = False,
) -> None:
    rows = []
    for y in range(height):
        row = bytearray([255] * width)
        if ink_border and y == 0:
            row[0] = 0
        elif 35 <= y < 85:
            row[35:85] = bytes([0] * 50)
        rows.append(b"\x00" + bytes(row))

    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return (
            struct.pack(">I", len(payload))
            + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(
            b"IHDR",
            struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0),
        )
        + chunk(b"IDAT", zlib.compress(b"".join(rows)))
        + chunk(b"IEND", b"")
    )


def test_bar_chart_uses_declared_fields_and_preserves_negative_values() -> None:
    spec = VisualizationSpec(
        kind="bar",
        title="Signed values",
        data=(
            {"label": "loss", "value": -3.0, "distractor": 99.0},
            {"label": "gain", "value": 2.0, "distractor": 101.0},
        ),
        encoding={
            "x": {"field": "label", "type": "nominal"},
            "y": {"field": "value", "type": "quantitative"},
        },
    )

    model = build_chart_model(spec)

    assert [point["y"] for point in model["points"]] == [-3.0, 2.0]
    assert model["y_domain"] == [-3.0, 2.0]


@pytest.mark.parametrize("kind", ["line", "scatter"])
def test_xy_charts_use_declared_numeric_encodings(kind: str) -> None:
    spec = VisualizationSpec(
        kind=kind,
        title="Observed series",
        data=(
            {"step": 10, "score": -1.5, "distractor": 500},
            {"step": 20, "score": 4.0, "distractor": 600},
        ),
        encoding={
            "x": {"field": "step", "type": "quantitative"},
            "y": {"field": "score", "type": "quantitative"},
        },
    )

    model = build_chart_model(spec)

    assert [point["x"] for point in model["points"]] == [10.0, 20.0]
    assert [point["y"] for point in model["points"]] == [-1.5, 4.0]


def test_heatmap_exports_source_bound_artifacts(tmp_path: Path) -> None:
    spec = VisualizationSpec(
        kind="heatmap",
        title="Confusion",
        data=(
            {"target": "a", "prediction": "a", "count": 2},
            {"target": "a", "prediction": "b", "count": 1},
        ),
        encoding={
            "x": {"field": "prediction", "type": "nominal"},
            "y": {"field": "target", "type": "nominal"},
            "color": {"field": "count", "type": "quantitative"},
        },
    )

    records = VisualizationExporter(tmp_path).export(
        spec,
        name="confusion",
        source_manifest={"dataset_sha256": "a" * 64},
    )

    assert {Path(record.path).name for record in records} == {
        "figure.pdf",
        "figure.tex",
        "caption.txt",
        "source.manifest.json",
    }
    assert (tmp_path / records[0].path).read_bytes().startswith(b"%PDF-1.4")
    pdf = (tmp_path / records[0].path).read_bytes()
    for expected in (
        b"prediction",
        b"target",
        b"count",
        b"(a)",
        b"(b)",
    ):
        assert expected in pdf


def test_unsupported_visualization_fails_closed() -> None:
    spec = VisualizationSpec(
        kind="pie",
        title="Unsupported",
        data=({"label": "a", "value": 1},),
        encoding={
            "x": {"field": "label", "type": "nominal"},
            "y": {"field": "value", "type": "quantitative"},
        },
    )

    with pytest.raises(ValueError, match="unsupported visualization kind"):
        build_chart_model(spec)


def test_scatter_legend_uses_the_same_category_colors(tmp_path: Path) -> None:
    spec = VisualizationSpec(
        kind="scatter",
        title="Classification",
        data=(
            {"step": 1, "score": 0.2, "correct": False},
            {"step": 2, "score": 0.8, "correct": True},
        ),
        encoding={
            "x": {"field": "step", "type": "quantitative"},
            "y": {"field": "score", "type": "quantitative"},
            "color": {"field": "correct", "type": "nominal"},
        },
    )

    records = VisualizationExporter(tmp_path).export(
        spec,
        name="category-colors",
        source_manifest={"source_sha256": "a" * 64},
    )
    pdf = (tmp_path / records[0].path).read_bytes()

    assert pdf.count(b"0.75 0.20 0.20 rg") >= 2
    assert pdf.count(b"0.15 0.25 0.55 rg") >= 2


def test_visual_page_check_reads_pixels_and_rejects_border_cropping(
    tmp_path: Path,
) -> None:
    clean = tmp_path / "clean.png"
    cropped = tmp_path / "cropped.png"
    _write_grayscale_png(clean)
    _write_grayscale_png(cropped, ink_border=True)

    assert inspect_rendered_page(clean)["result"] == "passed"
    assert inspect_rendered_page(cropped)["result"] == "failed"
    integrity = inspect_rendered_pages((clean,))
    assert integrity["schema"] == "paperforge.render-integrity/v1"
    assert integrity["passed"]

    pdf = tmp_path / "placeholder.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    with pytest.raises(
        ReleaseVerificationError,
        match="requires render integrity and structural review",
    ):
        write_page_inspection(
            tmp_path,
            pdf_path=pdf,
            rendered_pages=(clean,),
            reviewer="automated reviewer",
            inspection_kind="automated-structural",
            render_integrity=integrity,
        )


def test_pdftohtml_layout_fallback_parses_text_bounds() -> None:
    boxes = _pdftohtml_layout_boxes(
        b'<pdf2xml><page><text top="10" left="20" width="30" height="8">'
        b"observed result</text></page></pdf2xml>"
    )

    assert boxes == [(20.0, 10.0, 50.0, 18.0)]


def test_structural_page_review_rejects_intersecting_text_boxes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fitz = pytest.importorskip("fitz")
    document = fitz.open()
    page = document.new_page()
    for index in range(20):
        page.insert_text(
            (72 + index * 0.7, 72 + index * 0.05),
            "overlapping scientific result",
        )
    pdf = tmp_path / "overlap.pdf"
    rendered_page = tmp_path / "overlap-page-1.png"
    page.get_pixmap(dpi=120, alpha=False).save(rendered_page)
    document.save(pdf)
    document.close()
    rendered = (rendered_page,)
    integrity = inspect_rendered_pages(rendered)
    pdftotext = tmp_path / "pdftotext"
    pdftotext.write_text("", encoding="utf-8")
    layout = (
        "<doc><page>"
        + "".join(
            (
                f'<word xMin="{72 + index * 0.7}" yMin="{72 + index * 0.05}" '
                f'xMax="{210 + index * 0.7}" yMax="{84 + index * 0.05}">'
                "overlapping scientific result</word>"
            )
            for index in range(20)
        )
        + "</page></doc>"
    ).encode()

    def fake_pdftotext(
        argv: list[str],
        **_: object,
    ) -> SimpleNamespace:
        stdout = (
            layout
            if "-bbox-layout" in argv
            else ("overlapping scientific result " * 20).encode()
        )
        return SimpleNamespace(returncode=0, stdout=stdout)

    monkeypatch.setattr(visual_checks.subprocess, "run", fake_pdftotext)

    review = inspect_page_structure(
        pdf,
        rendered,
        render_integrity=integrity,
        pdftotext=pdftotext,
    )

    assert not review["passed"]
    assert review["pages"][0]["layout_overlap_clean"] is False
