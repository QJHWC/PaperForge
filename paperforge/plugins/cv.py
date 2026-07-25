from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from ._metrics import classification_metrics
from .base import DomainPlugin
from .contracts import ValidationIssue, VisualizationSpec


class CVPlugin(DomainPlugin):
    name = "cv"
    description = "Observed computer-vision classification evaluation"
    required_fields = ("target", "prediction")
    metric_methods = {
        "sample_count": "count of validated labeled samples",
        "class_count": "count of unique observed target or prediction labels",
        "accuracy": "correct predictions divided by validated samples",
        "precision_macro": "unweighted mean of per-class precision",
        "recall_macro": "unweighted mean of per-class recall",
        "f1_macro": "unweighted mean of per-class F1",
    }

    def _validate_row(
        self,
        row: Mapping[str, Any],
        row_index: int,
    ) -> tuple[dict[str, Any], tuple[ValidationIssue, ...]]:
        issues = self.missing_fields(row, self.required_fields, row_index)
        normalized = dict(row)
        for field in self.required_fields:
            if field not in row:
                continue
            value = row[field]
            if isinstance(value, bool) or not isinstance(value, str | int):
                issues.append(
                    ValidationIssue(
                        row_index=row_index,
                        field=field,
                        code="invalid_label",
                        message="label must be a string or integer",
                    )
                )
            elif isinstance(value, str) and not value:
                issues.append(
                    ValidationIssue(
                        row_index=row_index,
                        field=field,
                        code="empty_label",
                        message="label cannot be empty",
                    )
                )
        if "score" in row:
            score = row["score"]
            if (
                isinstance(score, bool)
                or not isinstance(score, int | float)
                or not math.isfinite(float(score))
                or not 0 <= float(score) <= 1
            ):
                issues.append(
                    ValidationIssue(
                        row_index=row_index,
                        field="score",
                        code="invalid_score",
                        message="score must be a finite number between 0 and 1",
                    )
                )
            else:
                normalized["score"] = float(score)
        return normalized, tuple(issues)

    def compute_metrics(
        self,
        rows: tuple[dict[str, Any], ...],
    ) -> dict[str, int | float]:
        metrics, _ = classification_metrics(
            [row["target"] for row in rows],
            [row["prediction"] for row in rows],
        )
        return metrics

    def build_visualizations(
        self,
        rows: tuple[dict[str, Any], ...],
        metrics: Mapping[str, int | float],
    ) -> tuple[VisualizationSpec, ...]:
        _, confusion = classification_metrics(
            [row["target"] for row in rows],
            [row["prediction"] for row in rows],
        )
        return (
            VisualizationSpec(
                kind="heatmap",
                title="Observed classification confusion matrix",
                description=(
                    "Counts are derived directly from validated target and prediction rows."
                ),
                data=confusion,
                encoding={
                    "x": {"field": "prediction", "type": "nominal"},
                    "y": {"field": "target", "type": "nominal"},
                    "color": {"field": "count", "type": "quantitative"},
                    "tooltip": [
                        {"field": "target"},
                        {"field": "prediction"},
                        {"field": "count"},
                    ],
                },
                metadata={"sample_count": len(rows)},
            ),
        )


ComputerVisionPlugin = CVPlugin
