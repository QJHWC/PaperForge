from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from ._metrics import mean
from .base import DomainPlugin
from .contracts import ValidationIssue, VisualizationSpec


class RoboticsPlugin(DomainPlugin):
    name = "robotics"
    description = "Observed robotics trial safety and error evaluation"
    required_fields = (
        "success",
        "collision",
        "position_error",
        "orientation_error",
    )
    metric_methods = {
        "sample_count": "count of validated observed robot trials",
        "success_rate": "successful observed trials divided by validated trials",
        "collision_rate": "observed collision trials divided by validated trials",
        "mean_position_error": "arithmetic mean of observed non-negative position errors",
        "mean_orientation_error": "arithmetic mean of observed non-negative orientation errors",
        "completion_time_count": "count of trials with observed completion time",
        "mean_completion_time": "arithmetic mean of observed non-negative completion times",
    }

    def _validate_row(
        self,
        row: Mapping[str, Any],
        row_index: int,
    ) -> tuple[dict[str, Any], tuple[ValidationIssue, ...]]:
        issues = self.missing_fields(row, self.required_fields, row_index)
        normalized = dict(row)
        for field in ("success", "collision"):
            if field in row and not isinstance(row[field], bool):
                issues.append(
                    ValidationIssue(
                        row_index=row_index,
                        field=field,
                        code="invalid_boolean",
                        message="field must be a boolean",
                    )
                )
        numeric_fields = ["position_error", "orientation_error"]
        if "completion_time" in row:
            numeric_fields.append("completion_time")
        for field in numeric_fields:
            if field not in row:
                continue
            value = row[field]
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(float(value))
                or float(value) < 0
            ):
                issues.append(
                    ValidationIssue(
                        row_index=row_index,
                        field=field,
                        code="invalid_non_negative_number",
                        message="field must be a finite non-negative number",
                    )
                )
            else:
                normalized[field] = float(value)
        return normalized, tuple(issues)

    def compute_metrics(
        self,
        rows: tuple[dict[str, Any], ...],
    ) -> dict[str, int | float]:
        metrics: dict[str, int | float] = {
            "sample_count": len(rows),
            "success_rate": sum(bool(row["success"]) for row in rows) / len(rows),
            "collision_rate": sum(bool(row["collision"]) for row in rows) / len(rows),
            "mean_position_error": mean([float(row["position_error"]) for row in rows]),
            "mean_orientation_error": mean([float(row["orientation_error"]) for row in rows]),
        }
        completion_times = [
            float(row["completion_time"]) for row in rows if "completion_time" in row
        ]
        if completion_times:
            metrics["completion_time_count"] = len(completion_times)
            metrics["mean_completion_time"] = mean(completion_times)
        return metrics

    def build_visualizations(
        self,
        rows: tuple[dict[str, Any], ...],
        metrics: Mapping[str, int | float],
    ) -> tuple[VisualizationSpec, ...]:
        data = [
            {
                "trial_index": index,
                "position_error": row["position_error"],
                "orientation_error": row["orientation_error"],
                "success": row["success"],
                "collision": row["collision"],
            }
            for index, row in enumerate(rows)
        ]
        return (
            VisualizationSpec(
                kind="scatter",
                title="Observed robotics trial errors",
                description=("Position and orientation errors are shown for each provided trial."),
                data=data,
                encoding={
                    "x": {"field": "position_error", "type": "quantitative"},
                    "y": {"field": "orientation_error", "type": "quantitative"},
                    "color": {"field": "success", "type": "nominal"},
                    "shape": {"field": "collision", "type": "nominal"},
                    "tooltip": [
                        {"field": "trial_index"},
                        {"field": "position_error"},
                        {"field": "orientation_error"},
                        {"field": "success"},
                        {"field": "collision"},
                    ],
                },
                metadata={"sample_count": len(rows)},
            ),
        )


RobotLearningPlugin = RoboticsPlugin
