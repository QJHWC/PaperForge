"""Numeric aggregation, Markdown table generation, and Pareto front utilities.

Framework-agnostic helpers for summarizing experiment results across seeds,
ablations, or any grouped dimension.
"""

from __future__ import annotations

from typing import Dict, Iterable, List

import numpy as np


def bounded(value: float, low: float, high: float) -> float:
    """Clamp *value* to [low, high]."""
    return float(max(low, min(high, value)))


def aggregate_numeric(rows: List[Dict[str, float]]) -> Dict[str, Dict[str, float]]:
    """Compute mean / stderr / min / max for every numeric column.

    Returns a dict with keys ``means``, ``stderrs``, ``mins``, ``maxs``,
    each mapping ``<col>_<stat>`` to the corresponding value.
    """
    if not rows:
        return {"means": {}, "stderrs": {}, "mins": {}, "maxs": {}}

    keys = [k for k, v in rows[0].items() if isinstance(v, (int, float))]
    out: Dict[str, Dict[str, float]] = {
        "means": {},
        "stderrs": {},
        "mins": {},
        "maxs": {},
    }
    for k in keys:
        vec = np.array([float(r[k]) for r in rows], dtype=float)
        out["means"][f"{k}_mean"] = float(np.mean(vec))
        out["stderrs"][f"{k}_stderr"] = float(
            np.std(vec, ddof=0) / np.sqrt(len(vec))
        )
        out["mins"][f"{k}_min"] = float(np.min(vec))
        out["maxs"][f"{k}_max"] = float(np.max(vec))
    return out


def to_markdown_table(rows: List[Dict[str, object]], float_fmt: str = ".6f") -> str:
    """Render a list of dicts as a Markdown table."""
    if not rows:
        return ""
    cols = list(rows[0].keys())
    lines = [
        "| " + " | ".join(cols) + " |",
        "| " + " | ".join(["---"] * len(cols)) + " |",
    ]
    for row in rows:
        vals = []
        for c in cols:
            v = row[c]
            if isinstance(v, float):
                vals.append(f"{v:{float_fmt}}")
            else:
                vals.append(str(v))
        lines.append("| " + " | ".join(vals) + " |")
    return "\n".join(lines) + "\n"


def to_latex_table(
    rows: List[Dict[str, object]],
    caption: str = "",
    label: str = "",
    float_fmt: str = ".4f",
) -> str:
    """Render a list of dicts as a LaTeX tabular environment."""
    if not rows:
        return ""
    cols = list(rows[0].keys())
    col_spec = "l" + "r" * (len(cols) - 1)
    lines = [
        r"\begin{table}[htbp]",
        r"\centering",
        f"\\caption{{{caption}}}" if caption else "",
        f"\\label{{{label}}}" if label else "",
        f"\\begin{{tabular}}{{{col_spec}}}",
        r"\toprule",
        " & ".join(cols) + r" \\",
        r"\midrule",
    ]
    for row in rows:
        vals = []
        for c in cols:
            v = row[c]
            if isinstance(v, float):
                vals.append(f"{v:{float_fmt}}")
            else:
                vals.append(str(v))
        lines.append(" & ".join(vals) + r" \\")
    lines += [r"\bottomrule", r"\end{tabular}", r"\end{table}"]
    return "\n".join(line for line in lines if line) + "\n"


def pareto_front(
    rows: Iterable[Dict[str, float]],
    x_key: str,
    y_key: str,
    minimize_x: bool = True,
    maximize_y: bool = True,
) -> List[Dict[str, float]]:
    """Extract the Pareto-optimal front from *rows*.

    Default: minimize *x_key* and maximize *y_key* (e.g. latency vs accuracy).
    """
    x_sign = 1 if minimize_x else -1
    y_sign = -1 if maximize_y else 1
    sorted_rows = sorted(rows, key=lambda r: (x_sign * float(r[x_key]), y_sign * float(r[y_key])))
    front: List[Dict[str, float]] = []
    best_y = -1e18 if maximize_y else 1e18
    for row in sorted_rows:
        y = float(row[y_key])
        if (maximize_y and y > best_y) or (not maximize_y and y < best_y):
            front.append(row)
            best_y = y
    return front
