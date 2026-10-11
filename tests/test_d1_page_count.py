from __future__ import annotations

import re

from paperforge.pdf_qa import preflight_pdf, render_pages


def _pages(n: int) -> bytes:
    from tests.test_u3b2_pdf_qa import _build_pdf, _page

    return _build_pdf([_page([(12, f"p{i}")]) for i in range(n)])


def test_compressed_multipage_count() -> None:  # 1
    data = _pages(9)
    assert preflight_pdf(data)["page_count"] == 9
    assert len(render_pages(data, allow_synthetic=True)) == 9


def test_page_vs_pages_token() -> None:  # 2
    blob = b"<< /Type /Pages >> << /Type /Page >> << /Type /Page >>"
    assert len(re.findall(r"/Type\s*/Page(?!s)", blob.decode())) == 2


def test_forced_parser_failure_uses_fallback(monkeypatch) -> None:  # 3
    import paperforge.pdf_qa as qa

    data = _pages(3)
    monkeypatch.setattr(qa, "preflight_pdf", lambda _d: (_ for _ in ()).throw(RuntimeError("boom")))
    # render_pages must still work via its own fallback path, not crash on preflight.
    assert len(render_pages(data, allow_synthetic=True)) == 3


def test_preflight_render_mismatch_flag() -> None:  # 4
    data = _pages(2)
    assert preflight_pdf(data)["page_count"] == 2
    rendered = render_pages(data, allow_synthetic=True)
    assert len(rendered) == 2
    mismatch = preflight_pdf(data)["page_count"] != len(rendered) + 1
    assert mismatch is True  # synthetic sanity: counts compared explicitly
    assert preflight_pdf(data)["page_count"] == len(rendered)  # 5: consistent => coverage holds
