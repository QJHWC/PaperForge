from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from ._metrics import mean, multiset_overlap
from .base import DomainPlugin
from .contracts import ValidationIssue, VisualizationSpec

_TOKEN_PATTERN = re.compile(r"\w+|[^\w\s]", re.UNICODE)


class NLPPlugin(DomainPlugin):
    name = "nlp"
    description = "Observed text prediction evaluation"
    required_fields = ("reference", "prediction")
    metric_methods = {
        "sample_count": "count of validated reference-prediction pairs",
        "exact_match": "fraction of predictions exactly equal to references",
        "token_precision": "macro mean multiset token precision after lowercase tokenization",
        "token_recall": "macro mean multiset token recall after lowercase tokenization",
        "token_f1": "macro mean multiset token F1 after lowercase tokenization",
    }

    @staticmethod
    def tokenize(text: str) -> tuple[str, ...]:
        return tuple(token.lower() for token in _TOKEN_PATTERN.findall(text))

    def _validate_row(
        self,
        row: Mapping[str, Any],
        row_index: int,
    ) -> tuple[dict[str, Any], tuple[ValidationIssue, ...]]:
        issues = self.missing_fields(row, self.required_fields, row_index)
        normalized = dict(row)
        for field in self.required_fields:
            if field in row and not isinstance(row[field], str):
                issues.append(
                    ValidationIssue(
                        row_index=row_index,
                        field=field,
                        code="invalid_text",
                        message="field must be a string",
                    )
                )
        return normalized, tuple(issues)

    def compute_metrics(
        self,
        rows: tuple[dict[str, Any], ...],
    ) -> dict[str, int | float]:
        exact: list[float] = []
        precision: list[float] = []
        recall: list[float] = []
        f1: list[float] = []
        for row in rows:
            reference = str(row["reference"])
            prediction = str(row["prediction"])
            exact.append(float(reference == prediction))
            row_precision, row_recall, row_f1 = multiset_overlap(
                self.tokenize(reference),
                self.tokenize(prediction),
            )
            precision.append(row_precision)
            recall.append(row_recall)
            f1.append(row_f1)
        return {
            "sample_count": len(rows),
            "exact_match": mean(exact),
            "token_precision": mean(precision),
            "token_recall": mean(recall),
            "token_f1": mean(f1),
        }

    def build_visualizations(
        self,
        rows: tuple[dict[str, Any], ...],
        metrics: Mapping[str, int | float],
    ) -> tuple[VisualizationSpec, ...]:
        data = [
            {"metric": metric, "value": value}
            for metric, value in metrics.items()
            if metric != "sample_count"
        ]
        return (
            VisualizationSpec(
                kind="bar",
                title="Observed NLP evaluation metrics",
                description=(
                    "Exact-match and token-overlap metrics computed from provided text rows."
                ),
                data=data,
                encoding={
                    "x": {"field": "metric", "type": "nominal"},
                    "y": {
                        "field": "value",
                        "type": "quantitative",
                        "scale": {"domain": [0, 1]},
                    },
                    "tooltip": [
                        {"field": "metric"},
                        {"field": "value", "format": ".4f"},
                    ],
                },
                metadata={"sample_count": len(rows)},
            ),
        )


NaturalLanguageProcessingPlugin = NLPPlugin
