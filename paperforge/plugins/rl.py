from __future__ import annotations

import math
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any

from ._metrics import mean, median
from .base import DomainPlugin
from .contracts import ValidationIssue, VisualizationSpec


class RLPlugin(DomainPlugin):
    name = "rl"
    description = "Observed reinforcement-learning transition and episode evaluation"
    required_fields = ("reward", "done")
    metric_methods = {
        "sample_count": "count of validated transitions",
        "transition_count": "count of validated transitions",
        "episode_count": "count of observed episode identifiers or done-delimited sequences",
        "mean_episode_return": "arithmetic mean of sums of observed episode rewards",
        "median_episode_return": "median of sums of observed episode rewards",
        "mean_episode_length": "arithmetic mean of observed transition counts per episode",
        "success_rate": "successful annotated episodes divided by annotated episodes",
        "annotated_episode_count": "count of episodes containing an observed success label",
    }

    def _validate_row(
        self,
        row: Mapping[str, Any],
        row_index: int,
    ) -> tuple[dict[str, Any], tuple[ValidationIssue, ...]]:
        issues = self.missing_fields(row, self.required_fields, row_index)
        normalized = dict(row)
        if "reward" in row:
            reward = row["reward"]
            if (
                isinstance(reward, bool)
                or not isinstance(reward, int | float)
                or not math.isfinite(float(reward))
            ):
                issues.append(
                    ValidationIssue(
                        row_index=row_index,
                        field="reward",
                        code="invalid_reward",
                        message="reward must be a finite number",
                    )
                )
            else:
                normalized["reward"] = float(reward)
        if "done" in row and not isinstance(row["done"], bool):
            issues.append(
                ValidationIssue(
                    row_index=row_index,
                    field="done",
                    code="invalid_done",
                    message="done must be a boolean",
                )
            )
        if "success" in row and not isinstance(row["success"], bool):
            issues.append(
                ValidationIssue(
                    row_index=row_index,
                    field="success",
                    code="invalid_success",
                    message="success must be a boolean",
                )
            )
        if "episode_id" in row and (
            isinstance(row["episode_id"], bool)
            or not isinstance(row["episode_id"], str | int)
            or row["episode_id"] == ""
        ):
            issues.append(
                ValidationIssue(
                    row_index=row_index,
                    field="episode_id",
                    code="invalid_episode_id",
                    message="episode_id must be a non-empty string or integer",
                )
            )
        if "timestep" in row and (
            isinstance(row["timestep"], bool)
            or not isinstance(row["timestep"], int)
            or row["timestep"] < 0
        ):
            issues.append(
                ValidationIssue(
                    row_index=row_index,
                    field="timestep",
                    code="invalid_timestep",
                    message="timestep must be a non-negative integer",
                )
            )
        return normalized, tuple(issues)

    @staticmethod
    def _episodes(
        rows: tuple[dict[str, Any], ...],
    ) -> OrderedDict[str, list[dict[str, Any]]]:
        episodes: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
        implicit_index = 0
        for row in rows:
            if "episode_id" in row:
                episode_id = f"observed:{row['episode_id']}"
            else:
                episode_id = f"implicit:{implicit_index}"
            episodes.setdefault(episode_id, []).append(row)
            if "episode_id" not in row and row["done"]:
                implicit_index += 1
        return episodes

    def compute_metrics(
        self,
        rows: tuple[dict[str, Any], ...],
    ) -> dict[str, int | float]:
        episodes = self._episodes(rows)
        returns = [
            math.fsum(float(row["reward"]) for row in transitions)
            for transitions in episodes.values()
        ]
        lengths = [float(len(transitions)) for transitions in episodes.values()]
        metrics: dict[str, int | float] = {
            "sample_count": len(rows),
            "transition_count": len(rows),
            "episode_count": len(episodes),
            "mean_episode_return": mean(returns),
            "median_episode_return": median(returns),
            "mean_episode_length": mean(lengths),
        }
        annotated: list[bool] = []
        for transitions in episodes.values():
            labels = [row["success"] for row in transitions if "success" in row]
            if labels:
                annotated.append(bool(labels[-1]))
        if annotated:
            metrics["annotated_episode_count"] = len(annotated)
            metrics["success_rate"] = sum(annotated) / len(annotated)
        return metrics

    def build_visualizations(
        self,
        rows: tuple[dict[str, Any], ...],
        metrics: Mapping[str, int | float],
    ) -> tuple[VisualizationSpec, ...]:
        data = []
        for index, (episode_id, transitions) in enumerate(self._episodes(rows).items()):
            data.append(
                {
                    "episode_index": index,
                    "episode_id": episode_id,
                    "return": math.fsum(float(row["reward"]) for row in transitions),
                    "length": len(transitions),
                    "complete": bool(transitions[-1]["done"]),
                }
            )
        return (
            VisualizationSpec(
                kind="line",
                title="Observed episode returns",
                description=("Episode returns are sums of rewards in provided transition rows."),
                data=data,
                encoding={
                    "x": {"field": "episode_index", "type": "ordinal"},
                    "y": {"field": "return", "type": "quantitative"},
                    "tooltip": [
                        {"field": "episode_id"},
                        {"field": "return"},
                        {"field": "length"},
                        {"field": "complete"},
                    ],
                },
                metadata={"transition_count": len(rows)},
            ),
        )

    def warnings(
        self,
        rows: tuple[dict[str, Any], ...],
        metrics: Mapping[str, int | float],
    ) -> tuple[str, ...]:
        incomplete = sum(
            not bool(transitions[-1]["done"]) for transitions in self._episodes(rows).values()
        )
        if incomplete:
            return (
                f"{incomplete} observed episode(s) do not end with done=True; "
                "their partial returns are reported as observed.",
            )
        return ()


ReinforcementLearningPlugin = RLPlugin
