from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from ._metrics import mean, regression_metrics
from .base import DomainPlugin
from .contracts import ValidationIssue, VisualizationSpec


class PhysicsMaterialPlugin(DomainPlugin):
    name = "physics-material"
    description = "Observed physics and material-property regression evaluation"
    required_fields = ("measured_value", "predicted_value")
    metric_methods = {
        "sample_count": "count of validated measured-predicted property rows",
        "mae": "mean absolute prediction error against measured values",
        "rmse": "root mean squared prediction error against measured values",
        "pearson_r": "Pearson correlation across measured-predicted pairs",
        "r_squared": "one minus residual sum of squares divided by measured total sum of squares",
        "relative_error_sample_count": "count of rows with nonzero measured value",
        "mean_relative_error": "mean absolute error divided by nonzero measured magnitude",
        "conservation_residual_count": "count of rows with observed conservation residuals",
        "mean_abs_conservation_residual": "mean absolute observed conservation residual",
    }

    @staticmethod
    def _canonicalize(row: Mapping[str, Any]) -> dict[str, Any]:
        normalized = dict(row)
        if "measured_value" not in normalized:
            if "target" in normalized:
                normalized["measured_value"] = normalized["target"]
            elif "observed" in normalized:
                normalized["measured_value"] = normalized["observed"]
        if "predicted_value" not in normalized:
            if "prediction" in normalized:
                normalized["predicted_value"] = normalized["prediction"]
            elif "predicted" in normalized:
                normalized["predicted_value"] = normalized["predicted"]
        return normalized

    def _validate_row(
        self,
        row: Mapping[str, Any],
        row_index: int,
    ) -> tuple[dict[str, Any], tuple[ValidationIssue, ...]]:
        normalized = self._canonicalize(row)
        issues = self.missing_fields(normalized, self.required_fields, row_index)
        fields = list(self.required_fields)
        if "conservation_residual" in normalized:
            fields.append("conservation_residual")
        for field in fields:
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
        measured = [float(row["measured_value"]) for row in rows]
        predicted = [float(row["predicted_value"]) for row in rows]
        metrics = regression_metrics(measured, predicted)
        relative_errors = [
            abs(prediction - target) / abs(target)
            for target, prediction in zip(measured, predicted, strict=True)
            if target != 0
        ]
        if relative_errors:
            metrics["relative_error_sample_count"] = len(relative_errors)
            metrics["mean_relative_error"] = mean(relative_errors)
        residuals = [
            abs(float(row["conservation_residual"]))
            for row in rows
            if "conservation_residual" in row
        ]
        if residuals:
            metrics["conservation_residual_count"] = len(residuals)
            metrics["mean_abs_conservation_residual"] = mean(residuals)
        return metrics

    def build_visualizations(
        self,
        rows: tuple[dict[str, Any], ...],
        metrics: Mapping[str, int | float],
    ) -> tuple[VisualizationSpec, ...]:
        data = [
            {
                "sample_index": index,
                "measured_value": row["measured_value"],
                "predicted_value": row["predicted_value"],
            }
            for index, row in enumerate(rows)
        ]
        return (
            VisualizationSpec(
                kind="scatter",
                title="Observed material or physical properties vs predictions",
                description=("Points correspond one-to-one with validated measured-property rows."),
                data=data,
                encoding={
                    "x": {"field": "measured_value", "type": "quantitative"},
                    "y": {"field": "predicted_value", "type": "quantitative"},
                    "tooltip": [
                        {"field": "sample_index"},
                        {"field": "measured_value"},
                        {"field": "predicted_value"},
                    ],
                },
                metadata={"sample_count": len(rows)},
            ),
        )

    def warnings(
        self,
        rows: tuple[dict[str, Any], ...],
        metrics: Mapping[str, int | float],
    ) -> tuple[str, ...]:
        zero_targets = sum(float(row["measured_value"]) == 0 for row in rows)
        if zero_targets:
            return (
                f"{zero_targets} row(s) with measured_value=0 were excluded from "
                "mean_relative_error only.",
            )
        return ()


PhysicsMaterialsPlugin = PhysicsMaterialPlugin
MaterialSciencePlugin = PhysicsMaterialPlugin
