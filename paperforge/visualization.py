from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .artifacts import ArtifactRecord, ArtifactStore, sha256_bytes
from .plugins.contracts import VisualizationSpec

VISUALIZATION_MANIFEST_SCHEMA = "paperforge.visualization-source/v1"
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def _pdf_text(value: object) -> str:
    return (
        str(value)
        .encode("ascii", errors="replace")
        .decode("ascii")
        .replace("\\", "\\\\")
        .replace("(", "\\(")
        .replace(")", "\\)")
    )


def _latex_text(value: object) -> str:
    replacements = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    return "".join(replacements.get(character, character) for character in str(value))


def _encoding_field(spec: VisualizationSpec, channel: str) -> tuple[str, str]:
    raw = spec.encoding.get(channel)
    if not isinstance(raw, Mapping):
        raise ValueError(f"{spec.kind} visualization requires {channel} encoding")
    field = str(raw.get("field") or "").strip()
    value_type = str(raw.get("type") or "").strip().lower()
    if not field or not value_type:
        raise ValueError(
            f"{spec.kind} visualization {channel} encoding is incomplete"
        )
    return field, value_type


def _finite_number(value: object, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"visualization field {field!r} must be numeric")
    rendered = float(value)
    if not math.isfinite(rendered):
        raise ValueError(f"visualization field {field!r} must be finite")
    return rendered


def _domain(values: list[float], *, include_zero: bool = False) -> tuple[float, float]:
    if include_zero:
        values = [*values, 0.0]
    minimum = min(values)
    maximum = max(values)
    if minimum == maximum:
        padding = max(abs(minimum) * 0.05, 1.0)
        return minimum - padding, maximum + padding
    return minimum, maximum


def build_chart_model(spec: VisualizationSpec) -> dict[str, Any]:
    """Validate visualization semantics and return a deterministic chart model."""

    kind = spec.kind.strip().lower()
    if kind not in {"bar", "line", "scatter", "heatmap"}:
        raise ValueError(f"unsupported visualization kind: {spec.kind}")
    x_field, x_type = _encoding_field(spec, "x")
    y_field, y_type = _encoding_field(spec, "y")
    color_field = ""
    if "color" in spec.encoding:
        color_field, _ = _encoding_field(spec, "color")

    if kind == "bar":
        if y_type != "quantitative":
            raise ValueError("bar visualization y encoding must be quantitative")
        bar_points: list[dict[str, Any]] = [
            {
                "x": str(row.get(x_field, "")),
                "y": _finite_number(row.get(y_field), field=y_field),
            }
            for row in spec.data
        ]
        if any(not point["x"] for point in bar_points):
            raise ValueError("bar visualization contains an empty x value")
        y_domain = _domain(
            [float(point["y"]) for point in bar_points],
            include_zero=True,
        )
        return {
            "kind": kind,
            "x_field": x_field,
            "y_field": y_field,
            "x_type": x_type,
            "y_type": y_type,
            "points": bar_points,
            "x_domain": [point["x"] for point in bar_points],
            "y_domain": list(y_domain),
        }

    if kind in {"line", "scatter"}:
        if y_type != "quantitative":
            raise ValueError(f"{kind} visualization y encoding must be quantitative")
        xy_points: list[dict[str, Any]] = []
        for index, row in enumerate(spec.data):
            raw_x = row.get(x_field)
            x = (
                _finite_number(raw_x, field=x_field)
                if x_type == "quantitative"
                else float(index)
            )
            point = {
                "x": x,
                "x_label": str(raw_x),
                "y": _finite_number(row.get(y_field), field=y_field),
            }
            if color_field:
                point["color"] = str(row.get(color_field))
            xy_points.append(point)
        return {
            "kind": kind,
            "x_field": x_field,
            "y_field": y_field,
            "x_type": x_type,
            "y_type": y_type,
            "color_field": color_field,
            "points": xy_points,
            "x_domain": list(
                _domain([float(point["x"]) for point in xy_points])
            ),
            "y_domain": list(
                _domain([float(point["y"]) for point in xy_points])
            ),
        }

    color_field, color_type = _encoding_field(spec, "color")
    if color_type != "quantitative":
        raise ValueError("heatmap color encoding must be quantitative")
    x_values = list(dict.fromkeys(str(row.get(x_field, "")) for row in spec.data))
    y_values = list(dict.fromkeys(str(row.get(y_field, "")) for row in spec.data))
    if any(not value for value in (*x_values, *y_values)):
        raise ValueError("heatmap contains an empty axis value")
    heatmap_cells: list[dict[str, Any]] = [
        {
            "x": str(row.get(x_field)),
            "y": str(row.get(y_field)),
            "value": _finite_number(row.get(color_field), field=color_field),
        }
        for row in spec.data
    ]
    return {
        "kind": kind,
        "x_field": x_field,
        "y_field": y_field,
        "color_field": color_field,
        "x_type": x_type,
        "y_type": y_type,
        "cells": heatmap_cells,
        "x_domain": x_values,
        "y_domain": y_values,
        "color_domain": list(
            _domain(
                [float(cell["value"]) for cell in heatmap_cells],
                include_zero=True,
            )
        ),
    }


def _scale(value: float, domain: list[float], low: float, high: float) -> float:
    minimum, maximum = domain
    return low + (value - minimum) * (high - low) / (maximum - minimum)


def _pdf_label(
    commands: list[str],
    *,
    x: float,
    y: float,
    text: object,
    size: int = 8,
) -> None:
    commands.extend(
        (
            "0.10 0.10 0.10 rg",
            "BT",
            f"/F1 {size} Tf",
            f"{x:.2f} {y:.2f} Td ({_pdf_text(text)}) Tj",
            "ET",
        )
    )


def _number_label(value: float) -> str:
    return f"{value:.4g}"


def _category_color(value: object) -> tuple[float, float, float]:
    normalized = str(value).strip().lower()
    if normalized in {"false", "0", "no"}:
        return 0.75, 0.20, 0.20
    if normalized in {"true", "1", "yes"}:
        return 0.15, 0.25, 0.55
    palette = (
        (0.12, 0.47, 0.71),
        (1.00, 0.50, 0.05),
        (0.17, 0.63, 0.17),
        (0.58, 0.40, 0.74),
        (0.55, 0.34, 0.29),
        (0.89, 0.47, 0.76),
    )
    index = int(hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:8], 16)
    return palette[index % len(palette)]


def _minimal_chart_pdf(spec: VisualizationSpec) -> bytes:
    model = build_chart_model(spec)
    left, bottom, right, top = 70.0, 80.0, 500.0, 350.0
    commands = [
        "q",
        "0.15 0.25 0.55 rg",
        f"{left:.2f} {bottom:.2f} 0.8 {top - bottom:.2f} re f",
        f"{left:.2f} {bottom:.2f} {right - left:.2f} 0.8 re f",
    ]
    kind = str(model["kind"])
    if kind == "bar":
        points = model["points"]
        width = (right - left) / len(points)
        zero_y = _scale(0.0, model["y_domain"], bottom, top)
        commands.append(
            f"{left:.2f} {zero_y:.2f} {right - left:.2f} 0.8 re f"
        )
        for index, point in enumerate(points):
            value_y = _scale(float(point["y"]), model["y_domain"], bottom, top)
            y = min(zero_y, value_y)
            height = max(1.0, abs(value_y - zero_y))
            x = left + index * width + 3.0
            commands.append(
                f"{x:.2f} {y:.2f} {max(2.0, width - 6.0):.2f} "
                f"{height:.2f} re f"
            )
            _pdf_label(
                commands,
                x=x,
                y=bottom - 15,
                text=point["x"],
            )
    elif kind in {"line", "scatter"}:
        points = model["points"]
        plotted = [
            (
                _scale(float(point["x"]), model["x_domain"], left, right),
                _scale(float(point["y"]), model["y_domain"], bottom, top),
                point,
            )
            for point in points
        ]
        if kind == "line":
            first_x, first_y, _ = plotted[0]
            commands.append(f"{first_x:.2f} {first_y:.2f} m")
            commands.extend(
                f"{x:.2f} {y:.2f} l" for x, y, _ in plotted[1:]
            )
            commands.append("2 w S")
        for x, y, point in plotted:
            red, green, blue = _category_color(point.get("color", ""))
            commands.append(f"{red:.2f} {green:.2f} {blue:.2f} rg")
            commands.append(f"{x - 2.5:.2f} {y - 2.5:.2f} 5 5 re f")
        if model["x_type"] != "quantitative":
            for x, _y, point in plotted:
                _pdf_label(
                    commands,
                    x=x - 5,
                    y=bottom - 15,
                    text=point["x_label"],
                )
        if model.get("color_field"):
            legend_values = list(
                dict.fromkeys(str(point.get("color", "")) for point in points)
            )
            _pdf_label(
                commands,
                x=510,
                y=top,
                text=model["color_field"],
                size=9,
            )
            for index, value in enumerate(legend_values):
                legend_y = top - 16 - index * 14
                red, green, blue = _category_color(value)
                commands.append(f"{red:.2f} {green:.2f} {blue:.2f} rg")
                commands.append(
                    f"510 {legend_y - 1:.2f} 7 7 re f"
                )
                _pdf_label(
                    commands,
                    x=520,
                    y=legend_y,
                    text=value,
                )
    else:
        x_values = model["x_domain"]
        y_values = model["y_domain"]
        cell_width = (right - left) / len(x_values)
        cell_height = (top - bottom) / len(y_values)
        color_min, color_max = model["color_domain"]
        for cell in model["cells"]:
            intensity = (
                (float(cell["value"]) - color_min) / (color_max - color_min)
            )
            red = 0.9 - 0.65 * intensity
            green = 0.95 - 0.55 * intensity
            blue = 1.0 - 0.25 * intensity
            x = left + x_values.index(cell["x"]) * cell_width
            y = bottom + y_values.index(cell["y"]) * cell_height
            commands.extend(
                (
                    f"{red:.3f} {green:.3f} {blue:.3f} rg",
                    f"{x:.2f} {y:.2f} {cell_width:.2f} "
                    f"{cell_height:.2f} re f",
                )
            )
        for index, value in enumerate(x_values):
            _pdf_label(
                commands,
                x=left + index * cell_width + 2,
                y=bottom - 15,
                text=value,
            )
        for index, value in enumerate(y_values):
            _pdf_label(
                commands,
                x=35,
                y=bottom + index * cell_height + (cell_height / 2),
                text=value,
            )
        _pdf_label(
            commands,
            x=510,
            y=top,
            text=model["color_field"],
            size=9,
        )
        for index in range(6):
            intensity = index / 5
            red = 0.9 - 0.65 * intensity
            green = 0.95 - 0.55 * intensity
            blue = 1.0 - 0.25 * intensity
            legend_y = bottom + index * 30
            commands.extend(
                (
                    f"{red:.3f} {green:.3f} {blue:.3f} rg",
                    f"510 {legend_y:.2f} 10 30 re f",
                )
            )
        _pdf_label(
            commands,
            x=524,
            y=bottom,
            text=_number_label(float(color_min)),
        )
        _pdf_label(
            commands,
            x=524,
            y=bottom + 145,
            text=_number_label(float(color_max)),
        )

    y_min, y_max = (
        model["color_domain"] if kind == "heatmap" else model["y_domain"]
    )
    if kind != "heatmap":
        for y_tick in (float(y_min), 0.0, float(y_max)):
            if float(y_min) <= y_tick <= float(y_max):
                y = _scale(y_tick, model["y_domain"], bottom, top)
                commands.append(
                    f"{left - 3:.2f} {y:.2f} 3 0.5 re f"
                )
                _pdf_label(
                    commands,
                    x=28,
                    y=y - 3,
                    text=_number_label(y_tick),
                )
    if kind in {"line", "scatter"} and model["x_type"] == "quantitative":
        for x_tick in (
            float(model["x_domain"][0]),
            float(model["x_domain"][1]),
        ):
            x = _scale(x_tick, model["x_domain"], left, right)
            _pdf_label(
                commands,
                x=x - 8,
                y=bottom - 15,
                text=_number_label(x_tick),
            )
    _pdf_label(
        commands,
        x=(left + right) / 2 - 15,
        y=bottom - 32,
        text=model["x_field"],
        size=10,
    )
    _pdf_label(
        commands,
        x=12,
        y=(bottom + top) / 2,
        text=model["y_field"],
        size=10,
    )
    commands.extend(
        (
            "BT",
            "/F1 15 Tf",
            f"60 402 Td ({_pdf_text(spec.title)}) Tj",
            "ET",
            "BT",
            "/F1 9 Tf",
            f"60 45 Td ({_pdf_text(spec.description[:100])}) Tj",
            "ET",
            "Q",
        )
    )
    content = ("\n".join(commands) + "\n").encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 432] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        b"<< /Length " + str(len(content)).encode("ascii") + b" >>\nstream\n"
        + content
        + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    payload = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, item in enumerate(objects, start=1):
        offsets.append(len(payload))
        payload.extend(f"{number} 0 obj\n".encode("ascii"))
        payload.extend(item)
        payload.extend(b"\nendobj\n")
    xref = len(payload)
    payload.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    payload.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        payload.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    payload.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(payload)


class VisualizationExporter:
    def __init__(
        self,
        workspace: str | Path,
        *,
        memory: Any | None = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.store = ArtifactStore(
            self.workspace,
            allowed_roots=("artifacts/visualizations",),
            allowed_suffixes=(".json", ".pdf", ".tex", ".txt"),
            allowed_kinds=("figure", "latex", "manifest"),
            memory=memory,
        )

    def export(
        self,
        spec: VisualizationSpec,
        *,
        name: str,
        source_manifest: Mapping[str, Any],
        run_id: str | None = None,
        workflow_id: str | None = None,
    ) -> tuple[ArtifactRecord, ...]:
        if not _SAFE_NAME.fullmatch(name):
            raise ValueError("visualization name contains unsafe characters")
        root = f"artifacts/visualizations/{name}"
        caption = spec.description.strip() or spec.title
        pdf_content = _minimal_chart_pdf(spec)
        spec_payload = json.dumps(
            spec.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        source_payload = json.dumps(
            source_manifest,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        spec_sha256 = sha256_bytes(spec_payload)
        source_sha256 = sha256_bytes(source_payload)
        chart_model = build_chart_model(spec)
        workflow_metadata = (
            {"workflow_id": workflow_id} if workflow_id is not None else {}
        )
        tex_content = (
            "\\begin{figure}[t]\n"
            "  \\centering\n"
            "  \\includegraphics[width=\\linewidth]{figure.pdf}\n"
            f"  \\caption{{{_latex_text(caption)}}}\n"
            "\\end{figure}\n"
        )
        records = [
            self.store.write_bytes(
                f"{root}/figure.pdf",
                pdf_content,
                kind="figure",
                run_id=run_id,
                media_type="application/pdf",
                metadata={
                    **workflow_metadata,
                    "spec_sha256": spec_sha256,
                },
            ),
            self.store.write_text(
                f"{root}/figure.tex",
                tex_content,
                kind="latex",
                run_id=run_id,
                media_type="application/x-tex",
                metadata=workflow_metadata,
            ),
            self.store.write_text(
                f"{root}/caption.txt",
                caption.rstrip() + "\n",
                kind="figure",
                run_id=run_id,
                media_type="text/plain",
                metadata=workflow_metadata,
            ),
        ]
        manifest = {
            "schema": VISUALIZATION_MANIFEST_SCHEMA,
            "name": name,
            "spec": spec.to_dict(),
            "source": dict(source_manifest),
            "chart_model": chart_model,
            "spec_sha256": spec_sha256,
            "source_sha256": source_sha256,
            "artifacts": [record.to_dict() for record in records],
        }
        records.append(
            self.store.write_json(
                f"{root}/source.manifest.json",
                manifest,
                kind="manifest",
                run_id=run_id,
                metadata={
                    **workflow_metadata,
                    "source_verified": True,
                },
            )
        )
        return tuple(records)


__all__ = [
    "VISUALIZATION_MANIFEST_SCHEMA",
    "VisualizationExporter",
    "build_chart_model",
]
