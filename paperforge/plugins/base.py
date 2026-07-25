from __future__ import annotations

import hashlib
import json
import math
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from typing import Any

from .contracts import (
    DataValidationError,
    EvidenceRecord,
    PluginResult,
    ValidationIssue,
    VisualizationSpec,
)


class DomainPlugin(ABC):
    name = "abstract"
    description = ""
    required_fields: tuple[str, ...] = ()
    metric_methods: Mapping[str, str] = {}

    @abstractmethod
    def _validate_row(
        self,
        row: Mapping[str, Any],
        row_index: int,
    ) -> tuple[dict[str, Any], tuple[ValidationIssue, ...]]: ...

    @abstractmethod
    def compute_metrics(
        self,
        rows: tuple[dict[str, Any], ...],
    ) -> dict[str, int | float]: ...

    @abstractmethod
    def build_visualizations(
        self,
        rows: tuple[dict[str, Any], ...],
        metrics: Mapping[str, int | float],
    ) -> tuple[VisualizationSpec, ...]: ...

    def check_row(
        self,
        row: Mapping[str, Any],
        *,
        row_index: int = 0,
    ) -> tuple[ValidationIssue, ...]:
        if not isinstance(row, Mapping):
            return (
                ValidationIssue(
                    row_index=row_index,
                    field=None,
                    code="invalid_type",
                    message="row must be a mapping",
                ),
            )
        _, issues = self._validate_row(row, row_index)
        return issues

    def validate_row(
        self,
        row: Mapping[str, Any],
        *,
        row_index: int = 0,
    ) -> dict[str, Any]:
        if not isinstance(row, Mapping):
            raise DataValidationError(
                [
                    ValidationIssue(
                        row_index=row_index,
                        field=None,
                        code="invalid_type",
                        message="row must be a mapping",
                    )
                ],
                plugin=self.name,
            )
        normalized, issues = self._validate_row(row, row_index)
        if issues:
            raise DataValidationError(issues, plugin=self.name)
        return normalized

    def validate_rows(
        self,
        rows: Iterable[Mapping[str, Any]],
    ) -> tuple[dict[str, Any], ...]:
        normalized_rows: list[dict[str, Any]] = []
        issues: list[ValidationIssue] = []
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping):
                issues.append(
                    ValidationIssue(
                        row_index=index,
                        field=None,
                        code="invalid_type",
                        message="row must be a mapping",
                    )
                )
                continue
            normalized, row_issues = self._validate_row(row, index)
            normalized_rows.append(normalized)
            issues.extend(row_issues)
        if not normalized_rows and not issues:
            issues.append(
                ValidationIssue(
                    row_index=0,
                    field=None,
                    code="empty_dataset",
                    message="at least one observed row is required",
                )
            )
        if issues:
            raise DataValidationError(issues, plugin=self.name)
        return tuple(normalized_rows)

    def build_evidence_records(
        self,
        rows: tuple[dict[str, Any], ...],
        metrics: Mapping[str, int | float],
    ) -> tuple[EvidenceRecord, ...]:
        sample_count = int(metrics.get("sample_count", len(rows)))
        canonical_rows = json.dumps(
            rows,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        dataset_sha256 = hashlib.sha256(canonical_rows).hexdigest()
        records: list[EvidenceRecord] = []
        for metric, value in metrics.items():
            method = self.metric_methods.get(
                metric,
                "deterministic aggregation over validated observed rows",
            )
            records.append(
                EvidenceRecord(
                    domain=self.name,
                    metric=metric,
                    value=value,
                    sample_count=sample_count,
                    method=method,
                    dataset_sha256=dataset_sha256,
                    eligible_for_claims=False,
                    metadata={
                        "validated_rows": len(rows),
                        "claim_eligibility": "requires-verified-experiment-receipt",
                    },
                )
            )
        return tuple(records)

    def warnings(
        self,
        rows: tuple[dict[str, Any], ...],
        metrics: Mapping[str, int | float],
    ) -> tuple[str, ...]:
        return ()

    def evidence_records(
        self,
        rows: Iterable[Mapping[str, Any]],
        metrics: Mapping[str, int | float] | None = None,
    ) -> tuple[EvidenceRecord, ...]:
        validated = self.validate_rows(rows)
        computed = dict(metrics) if metrics is not None else self.compute_metrics(validated)
        return self.build_evidence_records(validated, computed)

    def visualization_specs(
        self,
        rows: Iterable[Mapping[str, Any]],
        metrics: Mapping[str, int | float] | None = None,
    ) -> tuple[VisualizationSpec, ...]:
        validated = self.validate_rows(rows)
        computed = dict(metrics) if metrics is not None else self.compute_metrics(validated)
        return self.build_visualizations(validated, computed)

    def visualization_spec(
        self,
        rows: Iterable[Mapping[str, Any]],
        metrics: Mapping[str, int | float] | None = None,
    ) -> VisualizationSpec:
        return self.visualization_specs(rows, metrics)[0]

    def execute(self, rows: Iterable[Mapping[str, Any]]) -> PluginResult:
        return self.run(rows)

    def run(self, rows: Iterable[Mapping[str, Any]]) -> PluginResult:
        validated = self.validate_rows(rows)
        metrics = self.compute_metrics(validated)
        if not metrics:
            raise RuntimeError(f"{self.name} produced no metrics")
        for metric, value in metrics.items():
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise TypeError(f"{self.name}.{metric} is not numeric")
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError(f"{self.name}.{metric} is not finite")
        evidence = self.build_evidence_records(validated, metrics)
        visualizations = self.build_visualizations(validated, metrics)
        if not evidence or not visualizations:
            raise RuntimeError(f"{self.name} must emit evidence and visualization specifications")
        return PluginResult(
            plugin=self.name,
            validated_rows=len(validated),
            metrics=metrics,
            evidence=evidence,
            visualizations=visualizations,
            warnings=self.warnings(validated, metrics),
        )

    @staticmethod
    def missing_fields(
        row: Mapping[str, Any],
        required: Iterable[str],
        row_index: int,
    ) -> list[ValidationIssue]:
        return [
            ValidationIssue(
                row_index=row_index,
                field=field,
                code="missing",
                message="required field is missing",
            )
            for field in required
            if field not in row
        ]
