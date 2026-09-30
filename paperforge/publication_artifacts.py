from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .path_safety import safe_mkdir

TABLE_GENERATOR_VERSION = "paperforge.table-generator/v1"
FIGURE_GENERATOR_VERSION = "paperforge.figure-generator/v1"
FACT_GENERATOR_VERSION = "paperforge.fact-generator/v1"

_BANNED_FACT_WORDS = (
    "statistically significant", "equivalent", "robust",
    "best overall", "generalizable", "hardware validated", "superior",
)


class PublicationArtifactError(RuntimeError):
    pass


def _canonical(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _spec_digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(dict(payload))).hexdigest()


def _tex_escape(text: str) -> str:
    return (
        str(text).replace("\\", "\\textbackslash{}")
        .replace("&", "\\&").replace("%", "\\%").replace("$", "\\$")
        .replace("#", "\\#").replace("_", "\\_").replace("{", "\\{").replace("}", "\\}")
        .replace("~", "\\textasciitilde{}").replace("^", "\\textasciicircum{}")
    )


def _format_display(canonical: str | None, places: int | None) -> str:
    if canonical is None:
        return ""
    if places is None:
        return str(canonical)
    try:
        quantized = Decimal(str(canonical)).quantize(Decimal(10) ** -int(places))
    except (InvalidOperation, ValueError):
        return str(canonical)
    return format(quantized, "f")


@dataclass(frozen=True)
class TableSpec:
    ledger_id: str
    table_id: str = ""
    columns: tuple[str, ...] = ()
    row_ids: tuple[str, ...] = ()
    row_order: tuple[str, ...] = ()
    display_labels: Mapping[str, Any] = field(default_factory=dict)
    display_formats: Mapping[str, Any] = field(default_factory=dict)
    missing_display: str = "---"
    footnotes: tuple[str, ...] = ()
    highlight_rules: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if not str(self.ledger_id or "").strip():
            raise PublicationArtifactError("TableSpec ledger_id is required")
        if not self.columns:
            raise PublicationArtifactError("TableSpec columns are required")
        for rule in self.highlight_rules or ():
            if not isinstance(rule, Mapping) or rule.get("rule") not in {"max", "min"} or not rule.get("metric"):
                raise PublicationArtifactError("highlight rules must declare metric + max/min")
        object.__setattr__(self, "columns", tuple(str(c) for c in self.columns))
        object.__setattr__(self, "row_ids", tuple(str(r) for r in (self.row_ids or ())))
        object.__setattr__(self, "row_order", tuple(str(r) for r in (self.row_order or ())))
        object.__setattr__(self, "display_labels", dict(self.display_labels or {}))
        object.__setattr__(self, "display_formats", dict(self.display_formats or {}))
        object.__setattr__(self, "footnotes", tuple(str(f) for f in (self.footnotes or ())))
        object.__setattr__(self, "highlight_rules", tuple(dict(r) for r in (self.highlight_rules or ())))

    def to_canonical(self) -> dict[str, Any]:
        return {
            "schema": "paperforge.table-spec/v1",
            "ledger_id": self.ledger_id,
            "columns": list(self.columns),
            "row_ids": sorted(self.row_ids),
            "row_order": list(self.row_order),
            "display_labels": dict(sorted(self.display_labels.items())),
            "display_formats": dict(sorted(self.display_formats.items())),
            "missing_display": self.missing_display,
            "footnotes": list(self.footnotes),
            "highlight_rules": [dict(sorted(r.items())) for r in self.highlight_rules],
        }

    def digest(self) -> str:
        return _spec_digest(self.to_canonical())


@dataclass(frozen=True)
class FigureSpec:
    ledger_id: str
    figure_id: str = ""
    x_metric: str = ""
    y_metric: str = ""
    series_by: str = "measurement_kind"
    filters: Mapping[str, Any] = field(default_factory=dict)
    labels: Mapping[str, Any] = field(default_factory=dict)
    axis_units: Mapping[str, Any] = field(default_factory=dict)
    display_range: Mapping[str, Any] = field(default_factory=dict)
    annotation_rules: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if not str(self.ledger_id or "").strip():
            raise PublicationArtifactError("FigureSpec ledger_id is required")
        if not str(self.x_metric or "").strip() or not str(self.y_metric or "").strip():
            raise PublicationArtifactError("FigureSpec x_metric/y_metric are required")
        object.__setattr__(self, "filters", dict(self.filters or {}))
        object.__setattr__(self, "labels", dict(self.labels or {}))
        object.__setattr__(self, "axis_units", dict(self.axis_units or {}))
        object.__setattr__(self, "display_range", dict(self.display_range or {}))
        object.__setattr__(self, "annotation_rules", tuple(dict(r) for r in (self.annotation_rules or ())))

    def to_canonical(self) -> dict[str, Any]:
        return {
            "schema": "paperforge.figure-spec/v1",
            "ledger_id": self.ledger_id,
            "x_metric": self.x_metric,
            "y_metric": self.y_metric,
            "series_by": self.series_by,
            "filters": dict(sorted(self.filters.items())),
            "labels": dict(sorted(self.labels.items())),
            "axis_units": dict(sorted(self.axis_units.items())),
            "display_range": dict(sorted(self.display_range.items())),
            "annotation_rules": [dict(sorted(r.items())) for r in self.annotation_rules],
        }

    def digest(self) -> str:
        return _spec_digest(self.to_canonical())


@dataclass(frozen=True)
class FactSummarySpec:
    ledger_id: str
    fact_id: str = ""
    kind: str = "metric"
    row_id: str = ""
    base_row_id: str = ""
    compare_row_id: str = ""
    qualifiers: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.ledger_id or "").strip():
            raise PublicationArtifactError("FactSummarySpec ledger_id is required")
        if self.kind not in {"metric", "delta", "aggregation", "provenance"}:
            raise PublicationArtifactError(f"unknown fact kind: {self.kind}")
        for text in (self.fact_id, self.row_id, self.base_row_id, self.compare_row_id):
            lowered = str(text or "").lower()
            for banned in _BANNED_FACT_WORDS:
                if banned in lowered:
                    raise PublicationArtifactError(f"fact text must not claim: {banned}")
        object.__setattr__(self, "qualifiers", dict(self.qualifiers or {}))

    def to_canonical(self) -> dict[str, Any]:
        return {
            "schema": "paperforge.fact-summary-spec/v1",
            "ledger_id": self.ledger_id,
            "kind": self.kind,
            "row_id": self.row_id,
            "base_row_id": self.base_row_id,
            "compare_row_id": self.compare_row_id,
            "qualifiers": dict(sorted(self.qualifiers.items())),
        }

    def digest(self) -> str:
        return _spec_digest(self.to_canonical())


def _load_ledger(root: Path, ledger_id: str) -> dict[str, Any]:
    path = root / ".paperforge" / "ledgers" / ledger_id / "ledger.json"
    if not path.is_file():
        raise PublicationArtifactError(f"ledger missing: {ledger_id}")
    return json.loads(path.read_bytes().decode("utf-8"))


def _ledger_rows(ledger: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key in ("aggregate_rows", "detail_rows"):
        for row in ledger.get(key) or []:
            if isinstance(row, dict):
                rows.append(dict(row))
    return rows


def _register_outputs(
    root: Path, memory: Any, paths: Sequence[str], *, kind_map: Mapping[str, str],
    metadata: Mapping[str, Any], run_id: str | None = None,
) -> list[dict[str, Any]]:
    from .artifacts import ArtifactStore

    store = ArtifactStore(
        root, allowed_roots=("artifacts",), allowed_suffixes=(".csv", ".json", ".tex", ".pdf", ".svg", ".png"),
        allowed_kinds=("result", "manifest", "figure", "table", "report"), memory=memory,
    )
    records = []
    for rel in paths:
        kind = kind_map.get(rel.split("/")[-1], "result")
        records.append(store.register(rel, kind=kind, run_id=run_id, status="VERIFIED",
                                      media_type="application/octet-stream", metadata=dict(metadata)).to_dict())
    return records


def generate_table(
    workspace: str | Path, memory: Any, spec: TableSpec, *, run_id: str | None = None
) -> dict[str, Any]:
    root = Path(workspace).expanduser().resolve(strict=True)
    ledger = _load_ledger(root, spec.ledger_id)
    rows = _ledger_rows(ledger)
    by_id = {str(r.get("row_id")): r for r in rows}
    selected = [by_id[rid] for rid in spec.row_ids if rid in by_id] if spec.row_ids else list(rows)
    # Row order: explicit only; default preserves ledger order (no best-first).
    if spec.row_order:
        order = {rid: index for index, rid in enumerate(spec.row_order)}
        selected = sorted(selected, key=lambda r: (order.get(str(r.get("row_id")), 1 << 30), str(r.get("row_id"))))
    else:
        selected = sorted(selected, key=lambda r: str(r.get("row_id")))
    # Build structured rows: canonical values preserved; display only in tex.
    table_rows: list[dict[str, Any]] = []
    for row in selected:
        lineage = row.get("lineage") if isinstance(row.get("lineage"), list) else []
        table_rows.append({
            "row_id": row.get("row_id"),
            "metric_values": {col: None for col in spec.columns},
            "unit": row.get("unit"),
            "run_identity": row.get("run_identity"),
            "measurement_kind": row.get("measurement_kind"),
            "source_evidence_ids": row.get("source_evidence_ids"),
            "lineage": lineage,
        })
    # Map metric columns: each ledger row carries one metric; pivot by row.
    # For v1, columns are metrics and each row contributes its own metric cell.
    for table_row, ledger_row in zip(table_rows, selected, strict=False):
        metric = str(ledger_row.get("metric_name") or "")
        if metric in spec.columns:
            table_row["metric_values"][metric] = ledger_row.get("value_str")
    # Highlights: explicit max/min only, recorded in manifest.
    highlights: list[dict[str, Any]] = []
    for rule in spec.highlight_rules:
        metric, which = str(rule["metric"]), str(rule["rule"])
        candidates = [(r, (r["metric_values"] or {}).get(metric)) for r in table_rows]
        present = [(r, v) for r, v in candidates if v is not None]
        if not present:
            continue
        best = max(present, key=lambda pair: Decimal(str(pair[1]))) if which == "max" else min(present, key=lambda pair: Decimal(str(pair[1])))
        highlights.append({"metric": metric, "rule": which, "row_id": best[0]["row_id"]})
    spec_digest = spec.digest()
    table_id = spec.table_id or f"table-{spec_digest[:12]}"
    out_dir = safe_mkdir(root / "artifacts" / "tables" / table_id, anchor=root)
    # data.csv (canonical), table.json (structured), table.tex (display).
    header = ["row_id", *spec.columns]
    csv_lines = [",".join(header)]
    for table_row in table_rows:
        cells = [str(table_row["row_id"])]
        for col in spec.columns:
            value = (table_row["metric_values"] or {}).get(col)
            cells.append("" if value is None else str(value))
        csv_lines.append(",".join(cells))
    highlight_set = {(h["metric"], h["row_id"]) for h in highlights}
    tex_lines = [
        "\\begin{tabular}{l" + "l" * len(spec.columns) + "}",
        "\\hline",
        " & ".join(["row"] + [_tex_escape(str(spec.display_labels.get(c, c))) for c in spec.columns]) + " \\\\",
        "\\hline",
    ]
    for table_row in table_rows:
        cells = [_tex_escape(str(table_row["row_id"]))]
        for col in spec.columns:
            raw = (table_row["metric_values"] or {}).get(col)
            places = (spec.display_formats.get(col) or {}).get("places") if isinstance(spec.display_formats.get(col), dict) else spec.display_formats.get(col)
            display = spec.missing_display if raw is None else _format_display(str(raw), int(places) if places is not None else None)
            cell = _tex_escape(display)
            if (col, str(table_row["row_id"])) in highlight_set:
                cell = f"\\textbf{{{cell}}}"
            cells.append(cell)
        tex_lines.append(" & ".join(cells) + " \\\\")
    tex_lines += ["\\hline", "\\end{tabular}"]
    for footnote in spec.footnotes:
        tex_lines.append(f"% footnote: {_tex_escape(footnote)}")
    table_json = {"table_id": table_id, "columns": list(spec.columns), "rows": table_rows,
                  "highlight_rules": [dict(h) for h in highlights]}
    contents = {
        "data.csv": ("\n".join(csv_lines) + "\n").encode("utf-8"),
        f"{table_id}.tex" if False else "table.tex": ("\n".join(tex_lines) + "\n").encode("utf-8"),
        "table.json": (json.dumps(table_json, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    }
    manifest: dict[str, Any] = {
        "schema": "paperforge.table-manifest/v1",
        "table_id": table_id,
        "table_spec_digest": spec_digest,
        "ledger_id": spec.ledger_id,
        "ledger_digest": ledger.get("ledger_digest"),
        "generator_version": TABLE_GENERATOR_VERSION,
        "row_evidence_ids": sorted({eid for r in table_rows for eid in (r.get("source_evidence_ids") or [])}),
        "measurement_kinds": sorted({str(r.get("measurement_kind") or "") for r in table_rows if r.get("measurement_kind")}),
        "highlight_rules": [dict(h) for h in highlights],
        "artifact_stage": "publication_candidate",
        "publication_ready": False,
        "publication_ready_requirements": [
            "publication renderer", "visual/layout QA", "font/readability checks",
            "clipping/overflow checks", "required human review",
        ],
        "output_hashes": {},
    }
    output_paths: list[str] = []
    for name, data in contents.items():
        target = out_dir / name
        tmp = target.with_name(f".{name}.tmp")
        tmp.write_bytes(data)
        tmp.replace(target)
        manifest["output_hashes"][name] = hashlib.sha256(data).hexdigest()
        output_paths.append(f"artifacts/tables/{table_id}/{name}")
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    manifest_path = out_dir / "manifest.json"
    tmp = manifest_path.with_name(".manifest.json.tmp")
    tmp.write_bytes(manifest_bytes)
    tmp.replace(manifest_path)
    manifest["output_hashes"]["manifest.json"] = hashlib.sha256(manifest_bytes).hexdigest()
    # Rewrite manifest with its own hash included? Keep stable: hash covers outputs only.
    manifest_path.write_bytes((json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    output_paths.append(f"artifacts/tables/{table_id}/manifest.json")
    _register_outputs(root, memory, output_paths,
                      kind_map={"data.csv": "table", "table.json": "table", "table.tex": "table", "manifest.json": "manifest"},
                      metadata={"artifact_type": "table", "generator_version": TABLE_GENERATOR_VERSION,
                                "spec_digest": spec_digest, "ledger_id": spec.ledger_id,
                                "ledger_digest": ledger.get("ledger_digest"), "table_id": table_id},
                      run_id=run_id)
    return {"table_id": table_id, "spec_digest": spec_digest, "manifest": manifest}


def generate_figure(
    workspace: str | Path, memory: Any, spec: FigureSpec, *, run_id: str | None = None
) -> dict[str, Any]:
    root = Path(workspace).expanduser().resolve(strict=True)
    ledger = _load_ledger(root, spec.ledger_id)
    rows = _ledger_rows(ledger)
    # Filters: exact match on run_identity fields + metric presence.
    points: list[dict[str, Any]] = []
    for row in rows:
        identity = dict(row.get("run_identity") or {})
        if any(str(identity.get(k) or "") != str(v) for k, v in dict(spec.filters or {}).items()):
            continue
        points.append(row)
    # Build x/y pairs from rows sharing series identity: pair detail rows by evidence group.
    # v1: each ledger row is one metric observation; pair x and y by run/model/seed group.
    x_rows = [r for r in points if str(r.get("metric_name")) == spec.x_metric and r.get("value_str") is not None]
    y_rows = [r for r in points if str(r.get("metric_name")) == spec.y_metric and r.get("value_str") is not None]
    series: dict[str, list[dict[str, str]]] = {}
    for y in sorted(y_rows, key=lambda r: str(r.get("row_id"))):
        identity = dict(y.get("run_identity") or {})
        group = str(identity.get(spec.series_by) or y.get("measurement_kind") or "default")
        # Find matching x with same model/seed/device (excluding metric).
        match = next((
            x for x in x_rows
            if all(str((dict(x.get("run_identity") or {})).get(k) or "") == str(identity.get(k) or "")
                   for k in ("model_id", "seed", "device_id", "precision", "input_size"))
        ), None)
        if match is None:
            continue
        series.setdefault(group, []).append({
            "x": str(match.get("value_str")), "y": str(y.get("value_str")),
            "x_row_id": str(match.get("row_id")), "y_row_id": str(y.get("row_id")),
            "x_evidence": ",".join(match.get("source_evidence_ids") or []),
            "y_evidence": ",".join(y.get("source_evidence_ids") or []),
        })
    for group in series:
        series[group] = sorted(series[group], key=lambda p: (Decimal(p["x"]), Decimal(p["y"])))
    spec_digest = spec.digest()
    figure_id = spec.figure_id or f"figure-{spec_digest[:12]}"
    out_dir = safe_mkdir(root / "artifacts" / "figures" / figure_id, anchor=root)
    plot_data = {"figure_id": figure_id, "x_metric": spec.x_metric, "y_metric": spec.y_metric, "series": series}
    # Deterministic SVG (no timestamps) + minimal deterministic PDF wrapper.
    svg_points = []
    for group in sorted(series):
        for point in series[group]:
            svg_points.append(f'<circle cx="{point["x"]}" cy="{point["y"]}" data-series="{_tex_escape(group)}"/>')
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" version="1.1">'
        f"<title>{_tex_escape(figure_id)}</title>"
        + "".join(sorted(svg_points)) + "</svg>\n"
    )
    pdf_body = f"%PDF-1.4\n%{figure_id}\n".encode() + svg.encode("utf-8") + b"\n%%EOF\n"
    contents = {
        "plot_data.json": (json.dumps(plot_data, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        "plot_data.csv": ("series,x,y,x_row_id,y_row_id\n" + "".join(
            f"{group},{p['x']},{p['y']},{p['x_row_id']},{p['y_row_id']}\n"
            for group in sorted(series) for p in series[group]
        )).encode("utf-8"),
        "figure.svg": svg.encode("utf-8"),
        "figure.pdf": pdf_body,
    }
    manifest: dict[str, Any] = {
        "schema": "paperforge.figure-manifest/v1", "figure_id": figure_id,
        "figure_spec_digest": spec_digest, "ledger_id": spec.ledger_id,
        "ledger_digest": ledger.get("ledger_digest"), "generator_version": FIGURE_GENERATOR_VERSION,
        "plot_evidence_ids": sorted({eid for group in series.values() for p in group for eid in (p["x_evidence"].split(",") + p["y_evidence"].split(",")) if eid}),
        "measurement_kinds": sorted({str(r.get("measurement_kind") or "") for r in points if r.get("measurement_kind")}),
        "artifact_stage": "evidence_artifact",
        "render_quality": "deterministic_preview",
        "publication_ready": False,
        "publication_ready_requirements": [
            "publication renderer", "visual/layout QA", "font/readability checks",
            "clipping/overflow checks", "required human review",
        ],
        "output_hashes": {},
    }
    output_paths = []
    for name, data in contents.items():
        target = out_dir / name
        tmp = target.with_name(f".{name}.tmp")
        tmp.write_bytes(data)
        tmp.replace(target)
        manifest["output_hashes"][name] = hashlib.sha256(data).hexdigest()
        output_paths.append(f"artifacts/figures/{figure_id}/{name}")
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    (out_dir / "manifest.json").write_bytes(manifest_bytes)
    manifest["output_hashes"]["manifest.json"] = hashlib.sha256(manifest_bytes).hexdigest()
    (out_dir / "manifest.json").write_bytes((json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    output_paths.append(f"artifacts/figures/{figure_id}/manifest.json")
    _register_outputs(root, memory, output_paths,
                      kind_map={"plot_data.json": "figure", "plot_data.csv": "figure", "figure.svg": "figure", "figure.pdf": "figure", "manifest.json": "manifest"},
                      metadata={"artifact_type": "figure", "generator_version": FIGURE_GENERATOR_VERSION,
                                "spec_digest": spec_digest, "ledger_id": spec.ledger_id,
                                "ledger_digest": ledger.get("ledger_digest"), "figure_id": figure_id},
                      run_id=run_id)
    return {"figure_id": figure_id, "spec_digest": spec_digest, "manifest": manifest}


def generate_facts(
    workspace: str | Path, memory: Any, spec: FactSummarySpec, *, run_id: str | None = None
) -> dict[str, Any]:
    root = Path(workspace).expanduser().resolve(strict=True)
    ledger = _load_ledger(root, spec.ledger_id)
    rows = {str(r.get("row_id")): r for r in _ledger_rows(ledger)}
    for text in (spec.fact_id,):
        for banned in _BANNED_FACT_WORDS:
            if banned in str(text or "").lower():
                raise PublicationArtifactError(f"fact must not claim: {banned}")
    if spec.kind == "metric":
        row = rows.get(spec.row_id)
        if row is None:
            raise PublicationArtifactError(f"row missing: {spec.row_id}")
        fact: dict[str, Any] = {
            "fact_id": spec.fact_id or f"fact-{hashlib.sha256(spec.row_id.encode()).hexdigest()[:12]}",
            "kind": "metric",
            "subject": str((row.get("run_identity") or {}).get("model_id") or ""),
            "metric": row.get("metric_name"), "value": row.get("value_str"), "unit": row.get("unit"),
            "qualifiers": dict(spec.qualifiers), "ledger_id": spec.ledger_id,
            "ledger_digest": ledger.get("ledger_digest"),
            "evidence_ids": list(row.get("source_evidence_ids") or []),
            "lineage": row.get("lineage"),
            "presentation_hint": None,
        }
    elif spec.kind == "delta":
        base, compare = rows.get(spec.base_row_id), rows.get(spec.compare_row_id)
        if base is None or compare is None:
            raise PublicationArtifactError("delta rows missing")
        if base.get("metric_name") != compare.get("metric_name") or base.get("unit") != compare.get("unit"):
            raise PublicationArtifactError("delta requires same metric/unit")
        if base.get("value_str") is None or compare.get("value_str") is None:
            raise PublicationArtifactError("delta requires present values")
        delta = Decimal(str(base["value_str"])) - Decimal(str(compare["value_str"]))
        fact = {
            "fact_id": spec.fact_id or f"fact-delta-{hashlib.sha256((spec.base_row_id + spec.compare_row_id).encode()).hexdigest()[:12]}",
            "kind": "delta",
            "subject": str((base.get("run_identity") or {}).get("model_id") or ""),
            "predicate": "delta_vs",
            "object": str((compare.get("run_identity") or {}).get("model_id") or ""),
            "metric": base.get("metric_name"), "value": format(delta, "f"), "unit": base.get("unit"),
            "qualifiers": dict(spec.qualifiers), "ledger_id": spec.ledger_id,
            "ledger_digest": ledger.get("ledger_digest"),
            "evidence_ids": sorted(set(list(base.get("source_evidence_ids") or []) + list(compare.get("source_evidence_ids") or []))),
            "lineage": [base.get("lineage"), compare.get("lineage")],
            "presentation_hint": None,
        }
    elif spec.kind in {"aggregation", "provenance"}:
        row = rows.get(spec.row_id)
        if row is None:
            raise PublicationArtifactError(f"row missing: {spec.row_id}")
        fact = {
            "fact_id": spec.fact_id or f"fact-{spec.kind}-{hashlib.sha256(spec.row_id.encode()).hexdigest()[:12]}",
            "kind": spec.kind, "metric": row.get("metric_name"), "value": row.get("value_str"),
            "unit": row.get("unit"), "qualifiers": dict(spec.qualifiers),
            "ledger_id": spec.ledger_id, "ledger_digest": ledger.get("ledger_digest"),
            "evidence_ids": list(row.get("source_evidence_ids") or []),
            "lineage": row.get("lineage"), "presentation_hint": None,
        }
    else:
        raise PublicationArtifactError(f"unknown fact kind: {spec.kind}")
    spec_digest = spec.digest()
    fact_id = str(fact["fact_id"])
    out_dir = safe_mkdir(root / "artifacts" / "facts" / fact_id, anchor=root)
    facts_payload = {"facts": [fact], "spec": spec.to_canonical(), "spec_digest": spec_digest}
    contents = {"facts.json": (json.dumps(facts_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")}
    manifest: dict[str, Any] = {
        "schema": "paperforge.fact-manifest/v1", "fact_id": fact_id, "fact_spec_digest": spec_digest,
        "ledger_id": spec.ledger_id, "ledger_digest": ledger.get("ledger_digest"),
        "generator_version": FACT_GENERATOR_VERSION, "evidence_ids": list(fact["evidence_ids"]),
        "measurement_kinds": sorted({str(r.get("measurement_kind") or "") for r in rows.values() if r.get("measurement_kind")}),
        "artifact_stage": "publication_candidate",
        "publication_ready": False,
        "publication_ready_requirements": [
            "publication renderer", "required human review",
        ],
        "output_hashes": {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()},
    }
    output_paths = []
    for name, data in contents.items():
        target = out_dir / name
        tmp = target.with_name(f".{name}.tmp")
        tmp.write_bytes(data)
        tmp.replace(target)
        output_paths.append(f"artifacts/facts/{fact_id}/{name}")
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    (out_dir / "manifest.json").write_bytes(manifest_bytes)
    manifest["output_hashes"]["manifest.json"] = hashlib.sha256(manifest_bytes).hexdigest()
    (out_dir / "manifest.json").write_bytes((json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    output_paths.append(f"artifacts/facts/{fact_id}/manifest.json")
    _register_outputs(root, memory, output_paths,
                      kind_map={"facts.json": "result", "manifest.json": "manifest"},
                      metadata={"artifact_type": "fact", "generator_version": FACT_GENERATOR_VERSION,
                                "spec_digest": spec_digest, "ledger_id": spec.ledger_id,
                                "ledger_digest": ledger.get("ledger_digest"), "fact_id": fact_id},
                      run_id=run_id)
    return {"fact_id": fact_id, "spec_digest": spec_digest, "fact": fact, "manifest": manifest}


@dataclass(frozen=True)
class EvidenceLayerRef:
    """Typed evidence layer. A string label alone never bypasses ledger checks."""

    layer_id: str
    ledger_id: str
    ledger_digest: str
    purpose: str = "scientific"
    measurement_kinds: tuple[str, ...] = ()
    rationale: str = ""

    def __post_init__(self) -> None:
        if not str(self.layer_id or "").strip():
            raise PublicationArtifactError("layer_id is required")
        if not str(self.ledger_id or "").strip() or not str(self.ledger_digest or "").strip():
            raise PublicationArtifactError("layer ledger_id/digest are required")
        object.__setattr__(self, "measurement_kinds", tuple(str(k) for k in (self.measurement_kinds or ())))

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer_id": self.layer_id,
            "ledger_id": self.ledger_id,
            "ledger_digest": self.ledger_digest,
            "purpose": self.purpose,
            "measurement_kinds": list(self.measurement_kinds),
            "rationale": self.rationale,
        }


def _manifest_kinds(manifest: Mapping[str, Any]) -> set[str] | None:
    kinds = manifest.get("measurement_kinds")
    if kinds is None:
        return None
    return {str(k) for k in kinds if str(k).strip()}


def verify_publication_bundle(
    manifests: Sequence[Mapping[str, Any]],
    *,
    declared_layers: Sequence[EvidenceLayerRef | Mapping[str, Any]] | Mapping[str, Any] | None = None,
    layers: Sequence[EvidenceLayerRef | Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Shared-ledger invariant with typed layers and readiness breakdown."""
    layer_refs: list[dict[str, Any]] = []
    raw_layers = layers if layers is not None else declared_layers
    if isinstance(raw_layers, Mapping):
        raise PublicationArtifactError("declared_layers must be a typed layer list, not an arbitrary mapping")
    for entry in raw_layers or []:
        ref = entry.to_dict() if isinstance(entry, EvidenceLayerRef) else dict(entry)
        if not ref.get("layer_id") or not ref.get("ledger_id") or not ref.get("ledger_digest"):
            raise PublicationArtifactError("each layer needs layer_id + ledger_id + ledger_digest")
        layer_refs.append(ref)
    ledgers = [str(m.get("ledger_id") or "") for m in manifests]
    digests = [str(m.get("ledger_digest") or "") for m in manifests]
    artifact_integrity = bool(manifests) and all(ledgers) and all(digests)
    if not artifact_integrity:
        return {"consistent": False, "artifact_integrity": False, "ledger_consistency": False,
                "layer_consistency": False, "publication_readiness": "not_ready",
                "reason": "missing ledger_id/digest"}
    same = len(set(zip(ledgers, digests, strict=False))) == 1
    if same and not layer_refs:
        return {"consistent": True, "artifact_integrity": True, "ledger_consistency": True,
                "layer_consistency": True, "publication_readiness": "candidate_not_ready",
                "ledger_id": ledgers[0], "ledger_digest": digests[0]}
    if not layer_refs:
        return {"consistent": False, "artifact_integrity": True, "ledger_consistency": False,
                "layer_consistency": False, "publication_readiness": "not_ready",
                "reason": "table/figure/fact ledgers differ; declare typed layers explicitly"}
    # Typed layers: each artifact must belong to exactly one declared layer
    # (ledger_id + digest match); layers sharing one ledger are ambiguous.
    seen_ledger_keys = [(str(layer.get("ledger_id")), str(layer.get("ledger_digest"))) for layer in layer_refs]
    if len(set(seen_ledger_keys)) != len(seen_ledger_keys):
        return {"consistent": False, "artifact_integrity": True, "ledger_consistency": False,
                "layer_consistency": False, "publication_readiness": "not_ready",
                "reason": "duplicate layer ledgers are ambiguous"}
    for manifest in manifests:
        key = (str(manifest.get("ledger_id")), str(manifest.get("ledger_digest")))
        matches = [layer for layer in layer_refs if (str(layer.get("ledger_id")), str(layer.get("ledger_digest"))) == key]
        if len(matches) != 1:
            return {"consistent": False, "artifact_integrity": True, "ledger_consistency": False,
                    "layer_consistency": False, "publication_readiness": "not_ready",
                    "reason": f"artifact ledger {key} must match exactly one declared layer"}
        kinds = _manifest_kinds(manifest)
        if kinds is not None:
            allowed = {str(k) for k in matches[0].get("measurement_kinds") or []}
            if allowed and not kinds.issubset(allowed):
                return {"consistent": False, "artifact_integrity": True, "ledger_consistency": True,
                        "layer_consistency": False, "publication_readiness": "not_ready",
                        "reason": f"artifact kinds {sorted(kinds)} incompatible with layer {matches[0].get('layer_id')}"}
    return {"consistent": True, "artifact_integrity": True, "ledger_consistency": False,
            "layer_consistency": True, "publication_readiness": "candidate_not_ready",
            "layers": layer_refs, "note": "different evidence layers explicitly declared; no silent mixing"}


__all__ = [
    "FACT_GENERATOR_VERSION",
    "FIGURE_GENERATOR_VERSION",
    "TABLE_GENERATOR_VERSION",
    "EvidenceLayerRef",
    "FactSummarySpec",
    "FigureSpec",
    "PublicationArtifactError",
    "TableSpec",
    "generate_facts",
    "generate_figure",
    "generate_table",
    "verify_publication_bundle",
]
