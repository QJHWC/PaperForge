from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from test_u23b_artifacts import _seed, _service

from paperforge.publication_artifacts import FigureSpec, generate_figure
from paperforge.result_ledger import ResultLedgerQuery, freeze_ledger


def test_figure_pdf_is_real_deterministic_renderable_preview(tmp_path: Path) -> None:
    import pymupdf
    from pypdf import PdfReader

    service = _service(tmp_path)
    _seed(service, metric="latency_ms", value="20")
    _seed(service, metric="map", value="0.8", unit="score")
    ledger = freeze_ledger(tmp_path, service.memory, ResultLedgerQuery(purpose="scientific"))
    spec = FigureSpec(ledger_id=ledger["ledger_id"], x_metric="latency_ms", y_metric="map")
    out = generate_figure(tmp_path, service.memory, spec)
    path = tmp_path / "artifacts" / "figures" / out["figure_id"] / "figure.pdf"
    original = path.read_bytes()
    reader = PdfReader(BytesIO(original), strict=True)
    assert len(reader.pages) == 1
    assert "not publication ready" in reader.pages[0].extract_text()
    assert b" re f" in reader.pages[0].get_contents().get_data()
    with pymupdf.open(path) as pdf:
        page = pdf[0]
        assert page.get_drawings()
        assert page.get_pixmap().samples.count(255) < len(page.get_pixmap().samples)
    generate_figure(tmp_path, service.memory, spec)
    assert path.read_bytes() == original
    assert out["manifest"]["publication_ready"] is False

@pytest.mark.parametrize("label", ['model" data-unexpected="yes', 'model<&>', "model'&\"<tag>"])
def test_figure_svg_labels_are_data_not_markup(tmp_path: Path, label: str) -> None:
    service = _service(tmp_path)
    _seed(service, metric="latency_ms", value="20", run={"model_id": label})
    _seed(service, metric="map", value="0.8", unit="score", run={"model_id": label})
    ledger = freeze_ledger(tmp_path, service.memory, ResultLedgerQuery(purpose="scientific"))
    spec = FigureSpec(ledger_id=ledger["ledger_id"], figure_id="preview<&>",
                      x_metric="latency_ms", y_metric="map", series_by="model_id")
    first = generate_figure(tmp_path, service.memory, spec)
    path = tmp_path / "artifacts" / "figures" / first["figure_id"] / "figure.svg"
    svg = ET.fromstring(path.read_bytes())
    ns = {"svg": "http://www.w3.org/2000/svg"}
    assert svg.find("svg:title", ns).text == "preview<&>"
    circles = svg.findall("svg:circle", ns)
    assert circles
    for circle in circles:
        assert circle.attrib["data-series"] == label
        assert set(circle.attrib) == {"cx", "cy", "data-series"}
    assert all(element.tag in {f"{{{ns['svg']}}}{name}" for name in ("svg", "title", "circle")}
               for element in svg.iter())
    second = generate_figure(tmp_path, service.memory, spec)
    assert first["manifest"]["output_hashes"] == second["manifest"]["output_hashes"]
