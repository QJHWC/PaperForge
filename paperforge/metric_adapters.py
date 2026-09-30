from __future__ import annotations

import csv
import hashlib
import io
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from .metric_observation import (
    AdapterContext,
    MetricObservation,
    SourceLocator,
    UnsupportedMetricSource,
)
from .result_ledger import _decimal_canonical

METRIC_JSON_SCHEMA = "paperforge.metric-json/v1"
RUN_SUMMARY_SCHEMA = "paperforge.run-summary/v1"
CSV_METRIC_SCHEMA = "paperforge.csv-metric-table/v1"


class SemanticsMismatchError(UnsupportedMetricSource):
    """Raw source claims semantics conflicting with authoritative Evidence."""

    code = "SEMANTICS_MISMATCH"


class EvidenceMetricAdapter(Protocol):
    @property
    def adapter_id(self) -> str: ...

    @property
    def adapter_version(self) -> str: ...

    @property
    def supported_schema(self) -> str: ...

    @property
    def supported_media(self) -> tuple[str, ...]: ...

    def extract(
        self,
        source_bytes: bytes,
        source_relpath: str,
        file_hash: str,
        context: AdapterContext,
    ) -> list[MetricObservation]:
        ...


# Evidence/materialization metadata is authoritative for scientific semantics.
# Adapters may only extract metric/value/unit/locator; they must never let raw
# files override kind/origin/role/identity/provenance.
_AUTHORITATIVE_SEMANTIC_KEYS = (
    "measurement_kind",
    "acquisition_origin",
    "environment_role",
    "run_identity",
    "model_id",
    "input_size",
    "precision",
    "seed",
)


def assert_semantics_authority(payload: Mapping[str, Any], context: AdapterContext) -> None:
    for key in _AUTHORITATIVE_SEMANTIC_KEYS:
        if key not in payload:
            continue
        claimed = payload[key]
        if key == "run_identity":
            if isinstance(claimed, Mapping):
                for sub_key, sub_value in claimed.items():
                    expected = str((context.run_identity or {}).get(sub_key) or "")
                    if str(sub_value or "").strip() and str(sub_value).strip() != expected:
                        raise SemanticsMismatchError(
                            f"SEMANTICS_MISMATCH: source run_identity.{sub_key}={sub_value!r} "
                            f"conflicts with Evidence {expected!r}"
                        )
            continue
        expected_map = {
            "measurement_kind": context.measurement_kind,
            "acquisition_origin": context.acquisition_origin,
            "environment_role": context.environment_role,
            "model_id": str((context.run_identity or {}).get("model_id") or ""),
            "input_size": str((context.run_identity or {}).get("input_size") or ""),
            "precision": str((context.run_identity or {}).get("precision") or ""),
            "seed": str((context.run_identity or {}).get("seed") or ""),
        }
        expected = str(expected_map.get(key) or "")
        if str(claimed or "").strip() and str(claimed).strip() != expected:
            raise SemanticsMismatchError(
                f"SEMANTICS_MISMATCH: source {key}={claimed!r} conflicts with Evidence {expected!r}"
            )


def _observation(
    *,
    metric: str,
    raw_value: Any,
    unit: str,
    context: AdapterContext,
    locator: SourceLocator,
    adapter_id: str,
    adapter_version: str,
    file_hash: str = "",
) -> MetricObservation:
    canonical, lexical = _decimal_canonical(raw_value)
    return MetricObservation(
        metric_name=str(metric).strip(),
        canonical_value=canonical,
        source_lexical_value=str(lexical),
        unit=str(unit).strip(),
        evidence_id=context.evidence_id,
        run_identity=dict(context.run_identity or {}),
        measurement_kind=context.measurement_kind,
        acquisition_origin=context.acquisition_origin,
        environment_role=context.environment_role,
        acquisition_method=context.acquisition_method,
        method_version=context.method_version,
        parent_evidence_ids=tuple(context.parent_evidence_ids or ()),
        source_artifact_id=context.source_artifact_id,
        source_file_hash=file_hash,
        source_locator=locator,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
    )


@dataclass(frozen=True)
class ScalarMetricJsonAdapter:
    adapter_id: str = "scalar-metric-json"
    adapter_version: str = "v1"
    supported_schema: str = METRIC_JSON_SCHEMA
    supported_media: tuple[str, ...] = ("application/json",)

    def extract(self, source_bytes, source_relpath, file_hash, context):
        try:
            payload = json.loads(source_bytes.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise UnsupportedMetricSource(f"not JSON: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("schema") != METRIC_JSON_SCHEMA:
            raise UnsupportedMetricSource("schema mismatch for metric-json/v1")
        assert_semantics_authority(payload, context)
        for field in ("metric", "value", "unit"):
            if field not in payload or (isinstance(payload[field], str) and not payload[field].strip()):
                raise UnsupportedMetricSource(f"metric-json missing {field}")
        return [
            _observation(
                metric=payload["metric"], raw_value=payload["value"], unit=payload["unit"],
                context=context,
                locator=SourceLocator(kind="json", pointer=f"{source_relpath}#/value", detail="metric-json/v1"),
                adapter_id=self.adapter_id, adapter_version=self.adapter_version,
                file_hash=file_hash,
            )
        ]


@dataclass(frozen=True)
class RunSummaryJsonAdapter:
    adapter_id: str = "run-summary-json"
    adapter_version: str = "v1"
    supported_schema: str = RUN_SUMMARY_SCHEMA
    supported_media: tuple[str, ...] = ("application/json",)

    def extract(self, source_bytes, source_relpath, file_hash, context):
        try:
            payload = json.loads(source_bytes.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise UnsupportedMetricSource(f"not JSON: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("schema") != RUN_SUMMARY_SCHEMA:
            raise UnsupportedMetricSource("schema mismatch for run-summary/v1")
        assert_semantics_authority(payload, context)
        metrics = payload.get("metrics")
        if not isinstance(metrics, dict) or not metrics:
            raise UnsupportedMetricSource("run-summary missing metrics")
        observations: list[MetricObservation] = []
        for name in sorted(metrics):
            entry = metrics[name]
            if not isinstance(entry, dict) or "value" not in entry or "unit" not in entry:
                raise UnsupportedMetricSource(f"run-summary metric {name!r} incomplete")
            observations.append(
                _observation(
                    metric=name, raw_value=entry["value"], unit=entry["unit"],
                    context=context,
                    locator=SourceLocator(kind="json", pointer=f"{source_relpath}#/metrics/{name}/value"),
                    adapter_id=self.adapter_id, adapter_version=self.adapter_version,
                    file_hash=file_hash,
                )
            )
        return observations


@dataclass(frozen=True)
class CsvMetricTableAdapter:
    adapter_id: str = "csv-metric-table"
    adapter_version: str = "v1"
    supported_schema: str = CSV_METRIC_SCHEMA
    supported_media: tuple[str, ...] = ("text/csv",)

    def extract(self, source_bytes, source_relpath, file_hash, context):
        try:
            text = source_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise UnsupportedMetricSource(f"not UTF-8 CSV: {exc}") from exc
        reader = csv.DictReader(io.StringIO(text))
        if reader.fieldnames is None or [h.strip() for h in reader.fieldnames] != ["metric", "value", "unit"]:
            raise UnsupportedMetricSource("CSV header must be exactly metric,value,unit")
        observations: list[MetricObservation] = []
        for index, row in enumerate(reader, start=2):
            if not row.get("metric") or not row.get("value") or not row.get("unit"):
                raise UnsupportedMetricSource(f"CSV row {index} incomplete")
            observations.append(
                _observation(
                    metric=row["metric"], raw_value=row["value"], unit=row["unit"],
                    context=context,
                    locator=SourceLocator(kind="csv", pointer=f"{source_relpath}", row=index, column="value"),
                    adapter_id=self.adapter_id, adapter_version=self.adapter_version,
                    file_hash=file_hash,
                )
            )
        if not observations:
            raise UnsupportedMetricSource("CSV contains no metric rows")
        return observations


class MetricExtractorRegistry:
    def __init__(self, adapters: Sequence[EvidenceMetricAdapter] | None = None) -> None:
        self._adapters: list[EvidenceMetricAdapter] = list(
            adapters if adapters is not None else default_adapters()
        )

    def extract(
        self,
        source_bytes: bytes,
        source_relpath: str,
        context: AdapterContext,
        *,
        media_type: str = "",
    ) -> list[MetricObservation]:
        file_hash = hashlib.sha256(source_bytes).hexdigest()
        errors: list[str] = []
        for adapter in self._adapters:
            if media_type and media_type not in adapter.supported_media:
                continue
            try:
                return adapter.extract(source_bytes, source_relpath, file_hash, context)
            except SemanticsMismatchError:
                # Authority violation: fail closed immediately, never fall through.
                raise
            except UnsupportedMetricSource as exc:
                errors.append(f"{adapter.adapter_id}: {exc}")
                continue
        raise UnsupportedMetricSource(
            "UNSUPPORTED_METRIC_SOURCE: no registered adapter accepts this source; "
            + "; ".join(errors[:3])
        )


def default_adapters() -> list[EvidenceMetricAdapter]:
    return [ScalarMetricJsonAdapter(), RunSummaryJsonAdapter(), CsvMetricTableAdapter()]


__all__ = [
    "CSV_METRIC_SCHEMA",
    "METRIC_JSON_SCHEMA",
    "RUN_SUMMARY_SCHEMA",
    "CsvMetricTableAdapter",
    "EvidenceMetricAdapter",
    "MetricExtractorRegistry",
    "RunSummaryJsonAdapter",
    "ScalarMetricJsonAdapter",
    "SemanticsMismatchError",
    "assert_semantics_authority",
    "default_adapters",
]
