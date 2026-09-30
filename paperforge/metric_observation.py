from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from .evidence_semantics import EvidenceSemanticsError

METRIC_OBSERVATION_VERSION = "paperforge.metric-observation/v1"


@dataclass(frozen=True)
class SourceLocator:
    kind: str = ""
    pointer: str = ""
    row: int | None = None
    column: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MetricObservation:
    """Authoritative chain: Evidence -> Adapter -> Observation -> Ledger.

    from_ledger_snapshot is a read/compat helper only, not the production
    entry point. Table/Figure/Fact generators must consume ledgers built
    from adapter-normalized observations, never raw CSV/JSON.
    """

    schema: str = METRIC_OBSERVATION_VERSION
    metric_name: str = ""
    canonical_value: str = ""
    source_lexical_value: str = ""
    unit: str = ""
    evidence_id: str = ""
    run_identity: Mapping[str, Any] = None  # type: ignore[assignment]
    measurement_kind: str = ""
    acquisition_origin: str = ""
    environment_role: str = ""
    acquisition_method: str = ""
    method_version: str = ""
    parent_evidence_ids: tuple[str, ...] = ()
    source_artifact_id: str = ""
    source_file_hash: str = ""
    source_locator: SourceLocator | Mapping[str, Any] | None = None
    adapter_id: str = ""
    adapter_version: str = ""
    semantics_version: str = METRIC_OBSERVATION_VERSION

    def __post_init__(self) -> None:
        if not str(self.metric_name or "").strip():
            raise EvidenceSemanticsError("metric_name is required")
        if not str(self.unit or "").strip():
            raise EvidenceSemanticsError("unit is required")
        if not str(self.evidence_id or "").strip():
            raise EvidenceSemanticsError("evidence_id is required")
        if not str(self.canonical_value or "").strip():
            raise EvidenceSemanticsError("canonical_value is required")
        locator = self.source_locator
        if isinstance(locator, Mapping):
            locator = SourceLocator(
                kind=str(locator.get("kind") or ""),
                pointer=str(locator.get("pointer") or ""),
                row=locator.get("row"),
                column=str(locator.get("column") or ""),
                detail=str(locator.get("detail") or ""),
            )
            object.__setattr__(self, "source_locator", locator)
        elif locator is None:
            object.__setattr__(self, "source_locator", SourceLocator())
        object.__setattr__(self, "parent_evidence_ids", tuple(self.parent_evidence_ids or ()))
        object.__setattr__(self, "run_identity", dict(self.run_identity or {}))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["parent_evidence_ids"] = list(self.parent_evidence_ids)
        payload["run_identity"] = dict(self.run_identity)
        locator = payload.get("source_locator")
        if isinstance(locator, SourceLocator):
            payload["source_locator"] = locator.to_dict()
        return payload

    @classmethod
    def from_ledger_snapshot(cls, snapshot: Mapping[str, Any]) -> MetricObservation:
        return cls(
            metric_name=str(snapshot.get("metric_name") or ""),
            canonical_value=str(snapshot.get("value_str") or ""),
            source_lexical_value=str(snapshot.get("source_lexical_value") or ""),
            unit=str(snapshot.get("unit") or ""),
            evidence_id=str(snapshot.get("evidence_id") or ""),
            run_identity=dict(snapshot.get("run_identity") or {}),
            measurement_kind=str(snapshot.get("measurement_kind") or ""),
            acquisition_origin=str(snapshot.get("acquisition_origin") or ""),
            environment_role=str(snapshot.get("environment_role") or ""),
            acquisition_method=str(snapshot.get("method") or ""),
            method_version=str(snapshot.get("method_version") or ""),
            parent_evidence_ids=tuple(snapshot.get("parent_evidence_ids") or ()),
            source_artifact_id=str(snapshot.get("import_id") or ""),
            source_file_hash=str(snapshot.get("source_manifest_sha256") or ""),
        )


class UnsupportedMetricSource(ValueError):
    pass


@dataclass(frozen=True)
class AdapterContext:
    evidence_id: str
    run_identity: Mapping[str, Any]
    measurement_kind: str
    acquisition_origin: str
    environment_role: str
    acquisition_method: str = ""
    method_version: str = ""
    parent_evidence_ids: tuple[str, ...] = ()
    source_artifact_id: str = ""
    source_file_hash: str = ""


__all__ = [
    "METRIC_OBSERVATION_VERSION",
    "AdapterContext",
    "MetricObservation",
    "SourceLocator",
    "UnsupportedMetricSource",
]
