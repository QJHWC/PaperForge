from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class ValidationIssue:
    row_index: int
    field: str | None
    code: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class DataValidationError(ValueError):
    def __init__(
        self,
        issues: Sequence[ValidationIssue],
        *,
        plugin: str | None = None,
    ) -> None:
        self.issues = tuple(issues)
        self.plugin = plugin
        prefix = f"{plugin}: " if plugin else ""
        detail = "; ".join(
            f"row {issue.row_index}, {issue.field or 'row'}: {issue.message}"
            for issue in self.issues[:5]
        )
        if len(self.issues) > 5:
            detail += f"; and {len(self.issues) - 5} more issue(s)"
        super().__init__(f"{prefix}data validation failed: {detail}")


@dataclass(frozen=True)
class EvidenceRecord:
    domain: str
    metric: str
    value: int | float
    sample_count: int
    method: str
    source: str = "observed_rows"
    evidence_type: str = "EXPERIMENT_METRIC"
    simulated: bool = False
    dataset_sha256: str = ""
    eligible_for_claims: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.value, bool) or not isinstance(self.value, int | float):
            raise TypeError("evidence value must be numeric")
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError("evidence value must be finite")
        if self.sample_count < 0:
            raise ValueError("sample_count cannot be negative")
        if self.source != "observed_rows":
            raise ValueError("domain plugin evidence must originate from observed_rows")
        if self.simulated:
            raise ValueError("simulated evidence is forbidden")
        if len(self.dataset_sha256) != 64 or any(
            character not in "0123456789abcdef"
            for character in self.dataset_sha256.lower()
        ):
            raise ValueError("dataset_sha256 must be a 64-character hexadecimal digest")
        if not isinstance(self.eligible_for_claims, bool):
            raise TypeError("eligible_for_claims must be a boolean")
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "metric": self.metric,
            "value": self.value,
            "sample_count": self.sample_count,
            "method": self.method,
            "source": self.source,
            "evidence_type": self.evidence_type,
            "simulated": False,
            "dataset_sha256": self.dataset_sha256,
            "eligible_for_claims": self.eligible_for_claims,
            "metadata": dict(self.metadata),
        }

    def to_scientific_memory_kwargs(self) -> dict[str, Any]:
        return {
            "evidence_type": self.evidence_type,
            "excerpt": (
                f"{self.domain}.{self.metric}={self.value} computed from "
                f"{self.sample_count} validated observed row(s) using {self.method}."
            ),
            "config_scope": f"domain-plugin:{self.domain}",
            "metadata": self.to_dict(),
        }


@dataclass(frozen=True)
class VisualizationSpec:
    kind: str
    title: str
    data: Sequence[Mapping[str, Any]]
    encoding: Mapping[str, Any]
    description: str = ""
    spec_version: str = "1.0"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.kind or not self.title:
            raise ValueError("visualization kind and title must be non-empty")
        normalized_data = tuple(dict(row) for row in self.data)
        if not normalized_data:
            raise ValueError("visualization data must contain observed values")
        object.__setattr__(self, "data", normalized_data)
        object.__setattr__(self, "encoding", dict(self.encoding))
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "spec_version": self.spec_version,
            "kind": self.kind,
            "title": self.title,
            "description": self.description,
            "data": [dict(row) for row in self.data],
            "encoding": dict(self.encoding),
            "metadata": dict(self.metadata),
        }

    def to_vega_lite(self) -> dict[str, Any]:
        mark = {
            "bar": "bar",
            "line": "line",
            "scatter": "point",
            "heatmap": "rect",
        }.get(self.kind, self.kind)
        return {
            "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
            "title": self.title,
            "description": self.description,
            "data": {"values": [dict(row) for row in self.data]},
            "mark": mark,
            "encoding": dict(self.encoding),
        }


@dataclass(frozen=True)
class PluginResult:
    plugin: str
    validated_rows: int
    metrics: Mapping[str, int | float]
    evidence: Sequence[EvidenceRecord]
    visualizations: Sequence[VisualizationSpec]
    warnings: Sequence[str] = ()

    def __post_init__(self) -> None:
        if self.validated_rows < 1:
            raise ValueError("validated_rows must be positive")
        normalized_metrics = dict(self.metrics)
        for name, value in normalized_metrics.items():
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise TypeError(f"metric {name!r} must be numeric")
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError(f"metric {name!r} must be finite")
        object.__setattr__(self, "metrics", normalized_metrics)
        object.__setattr__(self, "evidence", tuple(self.evidence))
        object.__setattr__(self, "visualizations", tuple(self.visualizations))
        object.__setattr__(self, "warnings", tuple(self.warnings))

    def to_dict(self) -> dict[str, Any]:
        return {
            "plugin": self.plugin,
            "validated_rows": self.validated_rows,
            "metrics": dict(self.metrics),
            "evidence": [record.to_dict() for record in self.evidence],
            "visualizations": [visualization.to_dict() for visualization in self.visualizations],
            "warnings": list(self.warnings),
        }
