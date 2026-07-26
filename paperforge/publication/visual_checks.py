from __future__ import annotations

import hashlib
import shutil
import struct
import subprocess
import zlib
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from ..artifacts import sha256_file

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class VisualInspectionError(ValueError):
    pass


def _paeth(left: int, above: int, upper_left: int) -> int:
    estimate = left + above - upper_left
    left_distance = abs(estimate - left)
    above_distance = abs(estimate - above)
    upper_left_distance = abs(estimate - upper_left)
    if left_distance <= above_distance and left_distance <= upper_left_distance:
        return left
    if above_distance <= upper_left_distance:
        return above
    return upper_left


def _decode_png(path: Path) -> tuple[int, int, int, bytes]:
    content = path.read_bytes()
    if not content.startswith(PNG_SIGNATURE):
        raise VisualInspectionError("rendered page is not a PNG")
    offset = len(PNG_SIGNATURE)
    header: tuple[int, int, int, int, int, int, int] | None = None
    compressed = bytearray()
    while offset + 12 <= len(content):
        length = struct.unpack(">I", content[offset : offset + 4])[0]
        chunk_type = content[offset + 4 : offset + 8]
        data_start = offset + 8
        data_end = data_start + length
        crc_end = data_end + 4
        if crc_end > len(content):
            raise VisualInspectionError("PNG chunk leaves the file")
        chunk_data = content[data_start:data_end]
        expected_crc = struct.unpack(">I", content[data_end:crc_end])[0]
        actual_crc = zlib.crc32(chunk_type)
        actual_crc = zlib.crc32(chunk_data, actual_crc) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            raise VisualInspectionError("PNG chunk checksum mismatch")
        if chunk_type == b"IHDR":
            if len(chunk_data) != 13:
                raise VisualInspectionError("PNG header is invalid")
            header = struct.unpack(">IIBBBBB", chunk_data)
        elif chunk_type == b"IDAT":
            compressed.extend(chunk_data)
        elif chunk_type == b"IEND":
            break
        offset = crc_end
    if header is None or not compressed:
        raise VisualInspectionError("PNG image data is missing")
    width, height, depth, color_type, compression, filtering, interlace = header
    channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(color_type)
    if (
        width < 100
        or height < 100
        or depth != 8
        or channels is None
        or compression != 0
        or filtering != 0
        or interlace != 0
    ):
        raise VisualInspectionError("PNG format is unsupported or implausible")
    row_bytes = width * channels
    try:
        raw = zlib.decompress(bytes(compressed))
    except zlib.error as exc:
        raise VisualInspectionError("PNG image data cannot be decompressed") from exc
    expected_size = height * (row_bytes + 1)
    if len(raw) != expected_size:
        raise VisualInspectionError("PNG decompressed size is invalid")
    decoded = bytearray(height * row_bytes)
    previous = bytearray(row_bytes)
    source_offset = 0
    for row_index in range(height):
        filter_type = raw[source_offset]
        source_offset += 1
        encoded = raw[source_offset : source_offset + row_bytes]
        source_offset += row_bytes
        current = bytearray(row_bytes)
        for index, value in enumerate(encoded):
            left = current[index - channels] if index >= channels else 0
            above = previous[index]
            upper_left = previous[index - channels] if index >= channels else 0
            if filter_type == 0:
                decoded_value = value
            elif filter_type == 1:
                decoded_value = value + left
            elif filter_type == 2:
                decoded_value = value + above
            elif filter_type == 3:
                decoded_value = value + ((left + above) // 2)
            elif filter_type == 4:
                decoded_value = value + _paeth(left, above, upper_left)
            else:
                raise VisualInspectionError("PNG uses an unknown row filter")
            current[index] = decoded_value & 0xFF
        start = row_index * row_bytes
        decoded[start : start + row_bytes] = current
        previous = current
    return width, height, color_type, bytes(decoded)


def inspect_rendered_page(path: str | Path) -> dict[str, Any]:
    page = Path(path).expanduser().resolve()
    width, height, color_type, pixels = _decode_png(page)
    channels = {0: 1, 2: 3, 4: 2, 6: 4}[color_type]
    ink = 0
    border_ink = 0
    minimum_x, minimum_y = width, height
    maximum_x = maximum_y = -1
    for y in range(height):
        for x in range(width):
            offset = (y * width + x) * channels
            if color_type == 0:
                red = green = blue = pixels[offset]
                alpha = 255
            elif color_type == 2:
                red, green, blue = pixels[offset : offset + 3]
                alpha = 255
            elif color_type == 4:
                red = green = blue = pixels[offset]
                alpha = pixels[offset + 1]
            else:
                red, green, blue, alpha = pixels[offset : offset + 4]
            luminance = (299 * red + 587 * green + 114 * blue) // 1000
            if alpha > 16 and luminance < 245:
                ink += 1
                minimum_x = min(minimum_x, x)
                minimum_y = min(minimum_y, y)
                maximum_x = max(maximum_x, x)
                maximum_y = max(maximum_y, y)
                if x < 2 or y < 2 or x >= width - 2 or y >= height - 2:
                    border_ink += 1
    total = width * height
    ink_ratio = ink / total
    border_ratio = border_ink / max(ink, 1)
    passed = (
        0.0005 <= ink_ratio <= 0.90
        and ink > 0
        and border_ink == 0
        and minimum_x > 1
        and minimum_y > 1
        and maximum_x < width - 2
        and maximum_y < height - 2
    )
    return {
        "path": str(page),
        "sha256": sha256_file(page),
        "result": "passed" if passed else "failed",
        "width": width,
        "height": height,
        "ink_ratio": round(ink_ratio, 8),
        "border_ink_ratio": round(border_ratio, 8),
        "content_bounds": [
            minimum_x,
            minimum_y,
            maximum_x,
            maximum_y,
        ],
        "checks": {
            "not_blank": ink_ratio >= 0.0005,
            "not_saturated": ink_ratio <= 0.90,
            "not_cropped_at_border": border_ink == 0,
        },
    }


def inspect_rendered_pages(paths: Iterable[str | Path]) -> dict[str, Any]:
    pages = []
    for path in paths:
        try:
            pages.append(inspect_rendered_page(path))
        except (OSError, VisualInspectionError) as exc:
            pages.append(
                {
                    "path": str(Path(path).expanduser()),
                    "result": "failed",
                    "reason": str(exc),
                }
            )
    return {
        "schema": "paperforge.render-integrity/v1",
        "method": "decoded-pixel-basic-render-integrity",
        "passed": bool(pages)
        and all(page.get("result") == "passed" for page in pages),
        "pages": pages,
    }


def _pdftotext_layout_boxes(content: bytes) -> list[tuple[float, float, float, float]]:
    root = ElementTree.fromstring(content)
    boxes: list[tuple[float, float, float, float]] = []
    for element in root.iter():
        if not element.tag.endswith("word"):
            continue
        boxes.append(
            (
                round(float(element.attrib["xMin"]), 1),
                round(float(element.attrib["yMin"]), 1),
                round(float(element.attrib["xMax"]), 1),
                round(float(element.attrib["yMax"]), 1),
            )
        )
    return boxes


def _pdftohtml_layout_boxes(content: bytes) -> list[tuple[float, float, float, float]]:
    root = ElementTree.fromstring(content)
    boxes: list[tuple[float, float, float, float]] = []
    for element in root.iter():
        if not element.tag.endswith("text"):
            continue
        left = round(float(element.attrib["left"]), 1)
        top = round(float(element.attrib["top"]), 1)
        width = round(float(element.attrib["width"]), 1)
        height = round(float(element.attrib["height"]), 1)
        boxes.append((left, top, left + width, top + height))
    return boxes


def inspect_page_structure(
    pdf_path: str | Path,
    rendered_pages: Iterable[str | Path],
    *,
    render_integrity: Mapping[str, Any],
    expected_text_by_page: Mapping[int, Sequence[str]] | None = None,
    pdftotext: str | Path | None = None,
) -> dict[str, Any]:
    """Bind basic pixel checks to per-page text extraction and expectations."""

    pdf = Path(pdf_path).expanduser().resolve()
    pages = tuple(Path(path).expanduser().resolve() for path in rendered_pages)
    raw_integrity_pages = render_integrity.get("pages")
    tool = (
        Path(pdftotext).expanduser().resolve()
        if pdftotext is not None
        else Path(located).resolve()
        if (located := shutil.which("pdftotext"))
        else None
    )
    if (
        render_integrity.get("schema") != "paperforge.render-integrity/v1"
        or render_integrity.get("passed") is not True
        or not isinstance(raw_integrity_pages, list)
        or len(raw_integrity_pages) != len(pages)
        or not pdf.is_file()
        or tool is None
        or not tool.is_file()
    ):
        return {
            "schema": "paperforge.structural-page-review/v1",
            "method": "pdftotext-plus-render-integrity",
            "passed": False,
            "pages": [],
        }

    expected = dict(expected_text_by_page or {})
    records = []
    for page_number, (page, raw_integrity) in enumerate(
        zip(pages, raw_integrity_pages, strict=True),
        start=1,
    ):
        integrity = dict(raw_integrity) if isinstance(raw_integrity, Mapping) else {}
        try:
            completed = subprocess.run(
                [
                    str(tool),
                    "-f",
                    str(page_number),
                    "-l",
                    str(page_number),
                    str(pdf),
                    "-",
                ],
                check=False,
                capture_output=True,
                timeout=30,
            )
            text = completed.stdout.decode("utf-8", errors="replace")
            layout = subprocess.run(
                [
                    str(tool),
                    "-bbox-layout",
                    "-f",
                    str(page_number),
                    "-l",
                    str(page_number),
                    str(pdf),
                    "-",
                ],
                check=False,
                capture_output=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            text = ""
            completed = None
            layout = None
        boxes: list[tuple[float, float, float, float]] = []
        layout_valid = False
        layout_backend = "pdftotext-bbox-layout"
        if layout is not None and layout.returncode == 0:
            try:
                boxes = _pdftotext_layout_boxes(layout.stdout)
                layout_valid = bool(boxes)
            except (ElementTree.ParseError, KeyError, ValueError):
                layout_valid = False
        if not layout_valid and (html_tool := shutil.which("pdftohtml")):
            try:
                html_layout = subprocess.run(
                    [
                        html_tool,
                        "-f",
                        str(page_number),
                        "-l",
                        str(page_number),
                        "-xml",
                        "-hidden",
                        "-stdout",
                        str(pdf),
                    ],
                    check=False,
                    capture_output=True,
                    timeout=30,
                )
                if html_layout.returncode == 0:
                    boxes = _pdftohtml_layout_boxes(html_layout.stdout)
                    layout_valid = bool(boxes)
                    layout_backend = "pdftohtml-xml"
            except (
                OSError,
                subprocess.SubprocessError,
                ElementTree.ParseError,
                KeyError,
                ValueError,
            ):
                layout_valid = False
        overlap_count = 0
        active: list[tuple[float, float, float, float]] = []
        for box in sorted(boxes):
            active = [other for other in active if other[2] > box[0]]
            box_area = max(0.0, box[2] - box[0]) * max(
                0.0,
                box[3] - box[1],
            )
            for other in active:
                width = min(box[2], other[2]) - max(box[0], other[0])
                height = min(box[3], other[3]) - max(box[1], other[1])
                if width <= 0 or height <= 0:
                    continue
                other_area = max(0.0, other[2] - other[0]) * max(
                    0.0,
                    other[3] - other[1],
                )
                denominator = min(box_area, other_area)
                if denominator > 0 and (width * height) / denominator >= 0.25:
                    overlap_count += 1
            active.append(box)
        # pdftohtml groups inline mathematical fragments more coarsely than
        # pdftotext word boxes, so allow a small bounded number of local
        # intersections while still rejecting repeated overprinted text.
        overlap_limit = (
            max(5, len(boxes) // 25)
            if layout_backend == "pdftohtml-xml"
            else max(2, len(boxes) // 50)
        )
        expectations = [
            str(value).strip()
            for value in expected.get(page_number, ())
            if str(value).strip()
        ]
        expected_found = all(value in text for value in expectations)
        page_sha256 = sha256_file(page) if page.is_file() else ""
        bound = (
            integrity.get("result") == "passed"
            and integrity.get("sha256") == page_sha256
            and Path(str(integrity.get("path", ""))).expanduser().resolve()
            == page
        )
        records.append(
            {
                "page": page_number,
                "path": str(page),
                "sha256": page_sha256,
                "result": (
                    "passed"
                    if (
                        completed is not None
                        and completed.returncode == 0
                        and len(text.strip()) >= 20
                        and expected_found
                        and bound
                        and layout_valid
                        and overlap_count <= overlap_limit
                    )
                    else "failed"
                ),
                "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "text_characters": len(text.strip()),
                "expected_text": expectations,
                "expected_text_found": expected_found,
                "render_integrity_bound": bound,
                "layout_backend": layout_backend,
                "layout_boxes": len(boxes),
                "layout_overlap_count": overlap_count,
                "layout_overlap_limit": overlap_limit,
                "layout_overlap_clean": (
                    layout_valid and overlap_count <= overlap_limit
                ),
            }
        )
    return {
        "schema": "paperforge.structural-page-review/v1",
        "method": "pdftotext-plus-render-integrity",
        "passed": bool(records)
        and all(record["result"] == "passed" for record in records),
        "pages": records,
    }


__all__ = [
    "VisualInspectionError",
    "inspect_page_structure",
    "inspect_rendered_page",
    "inspect_rendered_pages",
]
