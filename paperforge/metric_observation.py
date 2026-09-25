from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

from .evidence_semantics import EvidenceSemanticsError

METRIC_OBSERVATION_VERSION = "paperforge.metric-observation/v1"


@dataclass(frozen=True)
class MetricObservation:
    """U2.3B integration contract only (no CSV/JSON adapters in U2.3A).

    Materialized Evidence -> MetricObservation -> Frozen Ledger -> Table/Figure.
    Table generators must never parse raw CSV/JSON directly.
    """

    metric_name: str
    canonical_value: str
    unit: str
    evidence_id: str
    run_identity: Mapping[str, Any]
    measurement_kind: str
    acquisition_method: str = ""
    method_version: str = ""
    parent_evidence_ids: tuple[str, ...] = ()
    source_reference: str = ""
    semantics_version: str = METRIC_OBSERVATION_VERSION

    def __post_init__(self) -> None:
        if not str(self.metric_name or "").strip():
            raise EvidenceSemanticsError("metric_name is required")
        if not str(self.unit or "").strip():
            raise EvidenceSemanticsError("unit is required")
        if not str(self.evidence_id or "").strip():
            raise EvidenceSemanticsError("evidence_id is required")
        object.__setattr__(self, "parent_evidence_ids", tuple(self.parent_evidence_ids or ()))
        object.__setattr__(self, "run_identity", dict(self.run_identity or {}))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["parent_evidence_ids"] = list(self.parent_evidence_ids)
        payload["run_identity"] = dict(self.run_identity)
        return payload

    @classmethod
    def from_ledger_snapshot(cls, snapshot: Mapping[str, Any]) -> MetricObservation:
        return cls(
            metric_name=str(snapshot.get("metric_name") or ""),
            canonical_value=str(snapshot.get("value_str") or ""),
            unit=str(snapshot.get("unit") or ""),
            evidence_id=str(snapshot.get("evidence_id") or ""),
            run_identity=dict(snapshot.get("run_identity") or {}),
            measurement_kind=str(snapshot.get("measurement_kind") or ""),
            acquisition_method=str(snapshot.get("method") or ""),
            method_version=str(snapshot.get("method_version") or ""),
            parent_evidence_ids=tuple(snapshot.get("parent_evidence_ids") or ()),
            source_reference=str(
                snapshot.get("import_id") or snapshot.get("source_manifest_sha256") or ""
            ),
        )


__all__ = ["METRIC_OBSERVATION_VERSION", "MetricObservation"]
