import hashlib
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from paperforge.references import ReferenceLoader, format_reference_context


def archive(xml):
    stream = BytesIO()
    with ZipFile(stream, "w", compression=ZIP_DEFLATED) as package:
        package.writestr("word/document.xml", xml)
        package.writestr("word/_rels/document.xml.rels", "never fetch external relationships")
    return stream.getvalue()


def test_docx_extracts_paragraphs_and_table_text_not_zip_bytes(tmp_path):
    raw = archive(b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                  b'<w:body><w:p><w:r><w:t>Useful prose.</w:t></w:r></w:p>'
                  b'<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Table cell.</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
                  b'</w:body></w:document>')
    (tmp_path / "ref.docx").write_bytes(raw)
    item = ReferenceLoader(tmp_path).load(["ref.docx"]).excerpts[0]
    assert item.excerpt == "Useful prose.\nTable cell."
    assert item.sha256 == hashlib.sha256(raw).hexdigest()
    assert item.needs_human_review is False
    assert "UNTRUSTED-SOURCE" in format_reference_context(ReferenceLoader(tmp_path).load(["ref.docx"]))


@pytest.mark.parametrize("raw", [b"not a zip", archive(b"broken XML"),
    archive(b'<!DOCTYPE x [<!ENTITY secret "unexpected">]><x>&secret;</x>')])
def test_malformed_or_entity_docx_is_not_prompt_context(tmp_path, raw):
    (tmp_path / "ref.docx").write_bytes(raw)
    item = ReferenceLoader(tmp_path).load(["ref.docx"]).excerpts[0]
    assert item.needs_human_review is True
    assert item.excerpt == ""


def test_docx_budget_truncates_extracted_text_explicitly(tmp_path):
    raw = archive(b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                  b'<w:body><w:p><w:r><w:t>Useful long prose.</w:t></w:r></w:p></w:body></w:document>')
    (tmp_path / "ref.docx").write_bytes(raw)
    item = ReferenceLoader(tmp_path).load(["ref.docx"], budget_bytes=6).excerpts[0]
    assert item.excerpt == "Useful"
    assert item.truncated is True
    assert item.bytes_read == 6


@pytest.mark.parametrize("control,separator", [("tab", "\t"), ("br", "\n"), ("cr", "\n")])
def test_docx_preserves_inline_control_boundaries(tmp_path, control, separator):
    raw = archive(
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:body><w:p><w:r><w:t>alpha</w:t>'
        f'<w:{control}/><w:t>beta</w:t></w:r></w:p></w:body></w:document>')
    (tmp_path / "ref.docx").write_bytes(raw)
    item = ReferenceLoader(tmp_path).load(["ref.docx"]).excerpts[0]
    assert item.excerpt == f"alpha{separator}beta"
    assert item.needs_human_review is False
