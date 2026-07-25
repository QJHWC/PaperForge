from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from ._metrics import regression_metrics
from .base import DomainPlugin
from .contracts import ValidationIssue, VisualizationSpec


class BioPlugin(DomainPlugin):
    name = "bio"
    description = "Observed biological assay regression evaluation"
    required_fields = ("target", "prediction")
    metric_methods = {
        "sample_count": "count of validated observed assay rows",
        "mae": "mean absolute difference between prediction and observed target",
        "rmse": "root mean squared difference between prediction and observed target",
        "pearson_r": "Pearson correlation across observed target-prediction pairs",
        "r_squared": "one minus residual sum of squares divided by observed total sum of squares",
    }

    @staticmethod
    def _canonicalize(row: Mapping[str, Any]) -> dict[str, Any]:
        normalized = dict(row)
        if "target" not in normalized and "observed" in normalized:
            normalized["target"] = normalized["observed"]
        if "prediction" not in normalized and "predicted" in normalized:
            normalized["prediction"] = normalized["predicted"]
        return normalized

    def _validate_row(
        self,
        row: Mapping[str, Any],
        row_index: int,
    ) -> tuple[dict[str, Any], tuple[ValidationIssue, ...]]:
        normalized = self._canonicalize(row)
        issues = self.missing_fields(normalized, self.required_fields, row_index)
        for field in self.required_fields:
            if field not in normalized:
                continue
            value = normalized[field]
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(float(value))
            ):
                issues.append(
                    ValidationIssue(
                        row_index=row_index,
                        field=field,
                        code="invalid_number",
                        message="field must be a finite number",
                    )
                )
            else:
                normalized[field] = float(value)
        return normalized, tuple(issues)

    def compute_metrics(
        self,
        rows: tuple[dict[str, Any], ...],
    ) -> dict[str, int | float]:
        return regression_metrics(
            [float(row["target"]) for row in rows],
            [float(row["prediction"]) for row in rows],
        )

    def build_visualizations(
        self,
        rows: tuple[dict[str, Any], ...],
        metrics: Mapping[str, int | float],
    ) -> tuple[VisualizationSpec, ...]:
        data = [
            {
                "sample_index": index,
                "target": row["target"],
                "prediction": row["prediction"],
            }
            for index, row in enumerate(rows)
        ]
        return (
            VisualizationSpec(
                kind="scatter",
                title="Observed biological targets vs predictions",
                description=(
                    "Each point is one validated assay row; no synthetic points are added."
                ),
                data=data,
                encoding={
                    "x": {"field": "target", "type": "quantitative"},
                    "y": {"field": "prediction", "type": "quantitative"},
                    "tooltip": [
                        {"field": "sample_index"},
                        {"field": "target"},
                        {"field": "prediction"},
                    ],
                },
                metadata={"sample_count": len(rows)},
            ),
        )


BiologyPlugin = BioPlugin
