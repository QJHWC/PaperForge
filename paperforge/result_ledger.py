from __future__ import annotations

import contextlib
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any

from .evidence_semantics import EvidenceSemantics
from .external_import import RUN_IDENTITY_SCHEMA, ScientificRunIdentity
from .models import utc_now

LEDGER_SCHEMA = "paperforge.result-ledger/v1"
QUERY_SCHEMA = "paperforge.result-ledger-query/v1"
GENERATOR_VERSION = "paperforge.result-ledger-generator/v1"
DECIMAL_AGGREGATION_VERSION = "paperforge.decimal-aggregation/v1"
DECIMAL_CONTEXT = Context(prec=50, rounding=ROUND_HALF_EVEN)
STD_ALGORITHM = "decimal_sqrt_variance/v1"

# Compatibility: DB column validation_report_sha256 stores validation_digest.
# New code uses validation_digest; see external_import._current_validation_report.


class LedgerError(RuntimeError):
    pass


class LedgerConflictError(LedgerError):
    pass


CROSSABLE_DIMENSIONS = frozenset(
    {
        "seed",
        "checkpoint_sha256",
        "dataset_id",
        "split_sha256",
        "device_id",
        "runtime",
        "runtime_version",
        "precision",
        "input_size",
        "batch_size",
        "timing_scope",
        "measurement_kind",
    }
)


@dataclass(frozen=True)
class AggregationSpec:
    metric: str
    group_by: tuple[str, ...] = ()
    reducer: str = "mean"
    ddof: int | None = None
    allow_cross: tuple[str, ...] = ()
    cross_intent: str = ""
    parameters: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.metric or "").strip():
            raise LedgerError("aggregation metric is required")
        if self.reducer not in {"mean", "std", "max", "min", "count"}:
            raise LedgerError(f"unsupported reducer: {self.reducer}")
        if self.reducer == "std" and self.ddof is None:
            raise LedgerError("std reducer requires explicit ddof")
        normalized_cross = tuple(str(g) for g in (self.allow_cross or ()))
        unknown = [d for d in normalized_cross if d not in CROSSABLE_DIMENSIONS]
        if unknown:
            raise LedgerError(f"unknown cross dimensions: {unknown}")
        object.__setattr__(self, "group_by", tuple(str(g) for g in (self.group_by or ())))
        object.__setattr__(self, "allow_cross", normalized_cross)
        object.__setattr__(self, "cross_intent", str(self.cross_intent or ""))
        object.__setattr__(self, "parameters", dict(self.parameters or {}))

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "group_by": list(self.group_by),
            "reducer": self.reducer,
            "ddof": self.ddof,
            "allow_cross": list(self.allow_cross),
            "cross_intent": self.cross_intent,
            "parameters": dict(self.parameters),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> AggregationSpec:
        return cls(
            metric=str(payload["metric"]),
            group_by=tuple(payload.get("group_by") or ()),
            reducer=str(payload.get("reducer") or "mean"),
            ddof=payload.get("ddof"),
            allow_cross=tuple(payload.get("allow_cross") or ()),
            cross_intent=str(payload.get("cross_intent") or ""),
            parameters=dict(payload.get("parameters") or {}),
        )


@dataclass(frozen=True)
class ResultLedgerQuery:
    purpose: str = "scientific"
    evidence_ids: tuple[str, ...] = ()
    metric_names: tuple[str, ...] = ()
    measurement_kinds: tuple[str, ...] = ()
    acquisition_origins: tuple[str, ...] = ()
    environment_roles: tuple[str, ...] = ()
    run_filters: Mapping[str, Any] = field(default_factory=dict)
    aggregations: tuple[AggregationSpec, ...] = ()
    missing_policy: str = "preserve"
    conflict_policy: str = "fail"
    sort_by: tuple[str, ...] = ("row_id",)
    unit_conversions: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if self.purpose not in {"scientific", "diagnostic"}:
            raise LedgerError(f"unknown purpose: {self.purpose}")
        if self.missing_policy != "preserve":
            raise LedgerError("v1 only supports missing_policy=preserve")
        if self.conflict_policy != "fail":
            raise LedgerError("v1 only supports conflict_policy=fail")
        object.__setattr__(self, "evidence_ids", tuple(str(e) for e in (self.evidence_ids or ())))
        object.__setattr__(self, "metric_names", tuple(str(e) for e in (self.metric_names or ())))
        object.__setattr__(self, "measurement_kinds", tuple(str(e) for e in (self.measurement_kinds or ())))
        object.__setattr__(self, "acquisition_origins", tuple(str(e) for e in (self.acquisition_origins or ())))
        object.__setattr__(self, "environment_roles", tuple(str(e) for e in (self.environment_roles or ())))
        object.__setattr__(self, "run_filters", dict(self.run_filters or {}))
        object.__setattr__(self, "sort_by", tuple(str(s) for s in (self.sort_by or ("row_id",))))
        object.__setattr__(self, "unit_conversions", tuple(dict(c) for c in (self.unit_conversions or ())))

    def to_canonical(self) -> dict[str, Any]:
        return {
            "schema": QUERY_SCHEMA,
            "purpose": self.purpose,
            "evidence_ids": sorted(self.evidence_ids),
            "metric_names": sorted(self.metric_names),
            "measurement_kinds": sorted(self.measurement_kinds),
            "acquisition_origins": sorted(self.acquisition_origins),
            "environment_roles": sorted(self.environment_roles),
            "run_filters": dict(sorted(self.run_filters.items())),
            "aggregations": [a.to_dict() for a in self.aggregations],
            "missing_policy": self.missing_policy,
            "conflict_policy": self.conflict_policy,
            "sort_by": list(self.sort_by),
            "unit_conversions": [dict(sorted(c.items())) for c in self.unit_conversions],
        }

    def digest(self) -> str:
        canonical = json.dumps(self.to_canonical(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _decimal_str(value: Any) -> str:
    """Legacy helper: canonical Decimal string (trailing zeros stripped)."""
    canonical, _ = _decimal_canonical(value)
    return canonical


def _decimal_canonical(value: Any) -> tuple[str, str]:
    """Return (canonical_value, source_lexical_value).

    Scientific identity uses the canonical form so "46.5" and "46.500" share
    the same numeric digest; the original lexical form stays as provenance.
    """
    lexical = str(value) if isinstance(value, str) else str(value)
    try:
        parsed = Decimal(lexical.strip())
    except (InvalidOperation, ValueError, TypeError, AttributeError) as exc:
        raise LedgerError(f"non-numeric ledger value: {value!r}") from exc
    if not parsed.is_finite():
        raise LedgerError(f"non-finite ledger value: {value!r}")
    normalized = parsed.normalize(DECIMAL_CONTEXT) if parsed != 0 else Decimal(0)
    return format(normalized, "f"), lexical.strip()


def _decimal_places(lexical: str) -> int:
    text = str(lexical or "").strip().lower().split("e")[0]
    if "." not in text:
        return 0
    return len(text.split(".", 1)[1])


def _load_evidence_rows(memory: Any) -> list[dict[str, Any]]:
    with memory.connect() as db:
        rows = db.execute("SELECT * FROM evidence ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def _snapshot_evidence(
    evidence_id: str, row: Mapping[str, Any], *, query: ResultLedgerQuery
) -> dict[str, Any] | None:
    """Build an immutable snapshot or return None when out of scope.

    Transcribed-only evidence never enters the scientific metric set.
    Fixture/synthetic are rejected for scientific purpose (allowed in
    diagnostic with explicit NOT PUBLICATION marking downstream).
    """
    try:
        metadata = json.loads(str(row.get("metadata_json") or "{}"))
    except (TypeError, ValueError):
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    try:
        semantics = EvidenceSemantics.from_metadata(
            {
                "measurement_kind": metadata.get("measurement_kind", "measured"),
                "acquisition_origin": metadata.get("acquisition_origin", "paperforge_run"),
                "environment_role": metadata.get("environment_role", "scientific"),
                "parent_evidence_ids": metadata.get("parent_evidence_ids") or (),
                "derivation_method": metadata.get("derivation_method"),
                "derivation_version": metadata.get("derivation_version"),
            }
        )
    except Exception:
        return None
    # Metric extraction: v1 requires explicit metric fields in metadata.
    metric_name = str(metadata.get("metric_name") or metadata.get("metric") or "").strip()
    unit = str(metadata.get("unit") or "").strip()
    has_value = "value_str" in metadata or "value" in metadata
    if not metric_name or not unit:
        # No metric identity: not part of any metric set.
        return None
    source_lexical_value: str | None = None
    source_decimal_places: int | None = None
    if not has_value:
        # Explicit missing observation: preserve as missing (never 0).
        value_str = None
        missing_reason = str(metadata.get("missing_reason") or "explicit missing")
    else:
        raw_value = metadata.get("value_str", metadata.get("value"))
        if raw_value is None or (isinstance(raw_value, str) and not raw_value.strip()):
            value_str = None
            missing_reason = str(metadata.get("missing_reason") or "explicit missing")
        else:
            try:
                value_str, source_lexical_value = _decimal_canonical(raw_value)
                source_decimal_places = _decimal_places(source_lexical_value)
            except LedgerError:
                value_str = None
            missing_reason = "" if value_str is not None else "non-numeric value"
    # Scientific purpose gates.
    if query.purpose == "scientific":
        if semantics.environment_role != "scientific":
            return None
        if semantics.measurement_kind == "transcribed":
            return None
        if query.measurement_kinds and semantics.measurement_kind not in query.measurement_kinds:
            return None
        if query.acquisition_origins and semantics.acquisition_origin not in query.acquisition_origins:
            return None
        if query.metric_names and metric_name not in query.metric_names:
            return None
    else:
        if query.metric_names and metric_name and metric_name not in query.metric_names:
            return None
    # Run identity: prefer stored identity, else reconstruct minimal.
    run_identity: dict[str, Any] = dict(metadata.get("run_identity") or {})
    if not run_identity:
        try:
            manifest_like = {
                "subject": metadata.get("subject") or {
                    "model_id": metadata.get("model_id", ""),
                    "checkpoint_sha256": metadata.get("checkpoint_sha256", ""),
                    "dataset_id": metadata.get("dataset_id", ""),
                    "split_sha256": metadata.get("split_sha256", ""),
                    "seed": metadata.get("seed", ""),
                },
                "execution": metadata.get("execution") or {
                    "device": metadata.get("device", ""),
                    "runtime": metadata.get("runtime", ""),
                    "runtime_version": metadata.get("runtime_version", ""),
                    "precision": metadata.get("precision", ""),
                    "input_size": metadata.get("input_size", ""),
                    "batch_size": metadata.get("batch_size", ""),
                    "timing_scope": metadata.get("timing_scope", ""),
                },
                "semantics": semantics.to_metadata(),
            }
            run_identity = ScientificRunIdentity.from_manifest(manifest_like).to_dict()
        except Exception:
            run_identity = {"schema": RUN_IDENTITY_SCHEMA}
    # Run filters (exact match on identity fields).
    for key, expected in dict(query.run_filters or {}).items():
        if str(run_identity.get(key) or "") != str(expected):
            return None
    snapshot_body: dict[str, Any] = {
        "evidence_id": evidence_id,
        "metric_name": metric_name,
        "value_str": value_str,
        "unit": unit,
        "missing_reason": missing_reason if value_str is None else "",
        "run_identity": run_identity,
        "measurement_kind": semantics.measurement_kind,
        "acquisition_origin": semantics.acquisition_origin,
        "environment_role": semantics.environment_role,
        "parent_evidence_ids": list(semantics.parent_evidence_ids),
        "method": semantics.derivation_method or str(metadata.get("method") or ""),
        "method_version": semantics.derivation_version or str(metadata.get("method_version") or ""),
        "import_id": metadata.get("import_id"),
        "materialization_key": metadata.get("materialization_key"),
        "source_manifest_sha256": metadata.get("manifest_sha256") or metadata.get("source_manifest_sha256"),
        "evidence_content_sha256": row.get("content_sha256"),
    }
    digest = hashlib.sha256(_canonical_json(snapshot_body)).hexdigest()
    stored: dict[str, Any] = {**snapshot_body, "evidence_snapshot_digest": digest}
    # Provenance-only lexical form; excluded from the digest above so
    # numerically identical values share one scientific identity.
    if source_lexical_value is not None:
        stored["source_lexical_value"] = source_lexical_value
        stored["source_decimal_places"] = source_decimal_places
    return stored


def _apply_unit_conversions(
    snapshots: list[dict[str, Any]], conversions: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    if not conversions:
        return snapshots
    rules = {(str(c.get("from") or ""), str(c.get("to") or "")): dict(c) for c in conversions}
    output: list[dict[str, Any]] = []
    for snap in snapshots:
        converted = dict(snap)
        for (source_unit, target_unit), rule in rules.items():
            if snap.get("unit") == source_unit and snap.get("metric_name") == rule.get("metric", snap.get("metric_name")):
                # Only Decimal-safe factors (e.g. s<->ms 1000, J<->mJ 1000).
                try:
                    factor = Decimal(str(rule.get("factor", "1")))
                    value = Decimal(str(snap["value_str"])) * factor if snap.get("value_str") else None
                except (InvalidOperation, TypeError) as exc:
                    raise LedgerError(f"invalid conversion factor: {rule}") from exc
                converted = {
                    **snap,
                    "value_str": format(value, "f") if value is not None else None,
                    "unit": target_unit,
                    "unit_conversion": {
                        "from": source_unit, "to": target_unit,
                        "method": rule.get("method", "explicit_factor"),
                        "version": rule.get("version", "v1"),
                    },
                }
        output.append(converted)
    return output


def _imported_metric_snapshots(
    root: Path, memory: Any, evidence_id: str, row: Mapping[str, Any],
    metadata: Mapping[str, Any], query: ResultLedgerQuery,
) -> list[dict[str, Any]]:
    from .external_import import _validate_metric_provenance, approval_valid, reconcile_imports
    from .metric_adapters import MetricExtractorRegistry
    from .metric_observation import AdapterContext, UnsupportedMetricSource
    from .path_safety import reject_symlink_components

    import_id = str(metadata.get("import_id") or "")
    if not import_id or import_id in {".", ".."} or any(c in import_id for c in "/\\:"):
        raise LedgerError("invalid import identity")
    valid, reason = approval_valid(root, memory, import_id)
    if not valid:
        raise LedgerError(f"import approval is not current: {reason}")
    if not reconcile_imports(root, memory)["consistent"]:
        raise LedgerError("import materialization integrity mismatch")
    base = root / ".paperforge" / "imports" / import_id
    manifest_path = reject_symlink_components(base / "manifest.json", anchor=root)
    manifest = json.loads(manifest_path.read_bytes())
    source_relative = Path(str(metadata.get("source_relpath") or ""))
    if (source_relative.is_absolute() or len(source_relative.parts) != 2
            or source_relative.parts[0] != "sources"
            or any(p in {".", ".."} for p in source_relative.parts)
            or "\\" in str(source_relative)):
        raise LedgerError("invalid imported metric source path")
    source = reject_symlink_components(base / source_relative, anchor=root)
    source_bytes = source.read_bytes()
    if hashlib.sha256(source_bytes).hexdigest() != metadata.get("source_sha256"):
        raise LedgerError("imported metric source integrity mismatch")
    semantics = EvidenceSemantics.from_metadata(dict(manifest.get("semantics") or {}))
    if EvidenceSemantics.from_metadata(metadata) != semantics:
        raise LedgerError("Evidence differs from approved semantics")
    execution = dict(manifest.get("execution") or {})
    metric_provenance = dict(execution.get("metric_provenance") or {})
    context = AdapterContext(
        evidence_id=evidence_id,
        run_identity=ScientificRunIdentity.from_manifest(manifest).to_dict(),
        measurement_kind=semantics.measurement_kind,
        acquisition_origin=semantics.acquisition_origin,
        environment_role=semantics.environment_role,
        acquisition_method=str(semantics.derivation_method or metric_provenance.get("acquisition_method") or ""),
        method_version=str(semantics.derivation_version or metric_provenance.get("method_version") or ""),
        parent_evidence_ids=semantics.parent_evidence_ids,
        source_artifact_id=import_id,
        source_file_hash=str(metadata.get("source_sha256") or ""),
    )
    try:
        observations = MetricExtractorRegistry().extract(
            source_bytes, source_relative.as_posix(), context)
    except UnsupportedMetricSource as exc:
        # An explicitly selected source must never disappear into an empty ledger.
        # Broad selection can include non-metric documents alongside metric logs.
        from .metric_adapters import SemanticsMismatchError

        if isinstance(exc, SemanticsMismatchError) or evidence_id in query.evidence_ids:
            raise
        return []
    snapshots = []
    for observation in observations:
        _validate_metric_provenance(semantics, observation.metric_name, metric_provenance)
        normalized = {
            **metadata, "metric_name": observation.metric_name,
            "value_str": observation.source_lexical_value, "unit": observation.unit,
            "run_identity": dict(observation.run_identity),
        }
        snapshot = _snapshot_evidence(
            evidence_id, {**row, "metadata_json": json.dumps(normalized)}, query=query)
        if snapshot is None:
            continue
        snapshot.update({
            "adapter_id": observation.adapter_id,
            "adapter_version": observation.adapter_version,
            "source_file_hash": observation.source_file_hash,
            "source_locator": observation.to_dict()["source_locator"],
        })
        body = {k: v for k, v in snapshot.items()
                if k not in {"evidence_snapshot_digest", "source_lexical_value", "source_decimal_places"}}
        snapshot["evidence_snapshot_digest"] = hashlib.sha256(_canonical_json(body)).hexdigest()
        snapshots.append(snapshot)
    return snapshots


def _aggregate(
    snapshots: list[dict[str, Any]], spec: AggregationSpec, *, purpose: str = "scientific"
) -> list[dict[str, Any]]:
    relevant = [s for s in snapshots if s.get("metric_name") == spec.metric]
    if not relevant:
        return []
    # Scientific v1 forbids cross-kind pooling even when explicitly listed.
    if purpose == "scientific" and "measurement_kind" in set(spec.allow_cross):
        raise LedgerError("scientific ledger v1 forbids cross measurement_kind aggregation")
    # Unit homogeneity unless explicit conversion already applied upstream.
    units = {str(s.get("unit") or "") for s in relevant if s.get("value_str") is not None}
    if len(units) > 1:
        raise LedgerError(
            f"unit mismatch for {spec.metric}: {sorted(units)}; provide explicit unit_conversions"
        )
    # Group rows. Default forbids silent cross-condition pooling; every
    # allowed dimension must be explicit with intent recorded per row.
    protected = set(CROSSABLE_DIMENSIONS)
    groups: dict[str, list[dict[str, Any]]] = {}
    for snap in relevant:
        identity = dict(snap.get("run_identity") or {})
        key_parts = [spec.metric]
        for dimension in sorted(set(spec.group_by)):
            key_parts.append(f"{dimension}={identity.get(dimension, '')}")
        key = "\x1f".join(key_parts)
        groups.setdefault(key, []).append(snap)
    rows: list[dict[str, Any]] = []
    for key in sorted(groups):
        members = sorted(groups[key], key=lambda s: str(s.get("evidence_id")))
        for dim in sorted(protected):
            distinct = {str((m.get("run_identity") or {}).get(dim) or "") for m in members}
            if len(distinct) > 1 and dim not in set(spec.group_by) and dim not in set(spec.allow_cross):
                raise LedgerError(
                    f"refusing to aggregate {spec.metric} across differing {dim}={sorted(distinct)}; "
                    "add it to group_by or allow_cross"
                )
        values = [m for m in members if m.get("value_str") is not None]
        missing = [m for m in members if m.get("value_str") is None]
        with localcontext(DECIMAL_CONTEXT) as ctx:
            ctx.prec = 50
            if spec.reducer == "count":
                result: str | None = str(len(values))
            elif not values:
                result = None
            elif spec.reducer == "mean":
                total = sum((Decimal(str(m["value_str"])) for m in values), Decimal(0))
                result = format(total / Decimal(len(values)), "f")
            elif spec.reducer == "std":
                assert spec.ddof is not None
                decimals = [Decimal(str(m["value_str"])) for m in values]
                n = len(decimals)
                denom = n - int(spec.ddof)
                if denom <= 0:
                    raise LedgerError(f"std needs n > ddof (n={n}, ddof={spec.ddof})")
                mean = sum(decimals, Decimal(0)) / Decimal(n)
                variance = sum(((d - mean) ** 2 for d in decimals), Decimal(0)) / Decimal(denom)
                result = format(variance.sqrt(ctx), "f")
            elif spec.reducer in {"max", "min"}:
                ordered = sorted(values, key=lambda m: Decimal(str(m["value_str"])))
                result = str(ordered[-1]["value_str"] if spec.reducer == "max" else ordered[0]["value_str"])
            else:
                raise LedgerError(f"unknown reducer: {spec.reducer}")
        first = members[0]
        row_id = hashlib.sha256(
            f"{spec.metric}\x1f{key}\x1f{spec.reducer}".encode()
        ).hexdigest()[:16]
        rows.append(
            {
                "row_id": f"row-{row_id}",
                "metric_name": spec.metric,
                "value_str": result,
                "unit": str(first.get("unit") or ""),
                "reducer": spec.reducer,
                "ddof": spec.ddof,
                "sample_count": len(values),
                "missing_count": len(missing),
                "source_evidence_ids": sorted(str(m["evidence_id"]) for m in members),
                "group": {g: (first.get("run_identity") or {}).get(g, "") for g in sorted(set(spec.group_by))},
                "allowed_cross": sorted(set(spec.allow_cross)),
                "cross_intent": spec.cross_intent,
                "decimal_aggregation_version": DECIMAL_AGGREGATION_VERSION,
                "std_algorithm": STD_ALGORITHM if spec.reducer == "std" else None,
                "run_identity": dict(first.get("run_identity") or {}),
                "measurement_kind": str(first.get("measurement_kind") or ""),
                "lineage": [
                    {
                        "evidence_id": m["evidence_id"],
                        "measurement_kind": m.get("measurement_kind"),
                        "method": m.get("method"),
                        "method_version": m.get("method_version"),
                        "parent_evidence_ids": m.get("parent_evidence_ids"),
                        "evidence_snapshot_digest": m.get("evidence_snapshot_digest"),
                    }
                    for m in members
                ],
            }
        )
    return sorted(rows, key=lambda r: str(r["row_id"]))


def freeze_ledger(
    workspace: str | Path,
    memory: Any,
    query: ResultLedgerQuery,
    *,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Select→canonicalize→aggregate→freeze→register. No Table/Figure output."""
    root = Path(workspace).expanduser().resolve(strict=True)
    if query.evidence_ids:
        with memory.connect() as db:
            rows = [
                dict(db.execute("SELECT * FROM evidence WHERE id=?", (eid,)).fetchone() or {})
                for eid in query.evidence_ids
            ]
        rows = [r for r in rows if r.get("id")]
        evidence_map = {str(r["id"]): r for r in rows}
    else:
        evidence_map = {str(r["id"]): r for r in _load_evidence_rows(memory) if r.get("id")}
    snapshots: list[dict[str, Any]] = []
    for evidence_id in sorted(evidence_map):
        row = evidence_map[evidence_id]
        try:
            metadata = json.loads(str(row.get("metadata_json") or "{}"))
        except (ValueError, TypeError):
            metadata = {}
        if isinstance(metadata, dict) and metadata.get("import_id"):
            snapshots.extend(_imported_metric_snapshots(root, memory, evidence_id, row, metadata, query))
            continue
        snap = _snapshot_evidence(evidence_id, row, query=query)
        if snap is not None:
            snapshots.append(snap)
    # Conflict policy fail: same (metric, run identity, kind) with different values.
    # Explicit max/min reducers are the only allowed resolution and must be
    # declared in the query (never silent latest/best).
    explicit_extremes = {
        str(spec.metric)
        for spec in query.aggregations
        if spec.reducer in {"max", "min"}
    }
    seen: dict[str, dict[str, Any]] = {}
    for snap in snapshots:
        if snap.get("value_str") is None:
            continue
        if str(snap.get("metric_name")) in explicit_extremes:
            continue
        key = json.dumps(
            {
                "metric": snap.get("metric_name"),
                "identity": snap.get("run_identity"),
                "kind": snap.get("measurement_kind"),
                "unit": snap.get("unit"),
            },
            sort_keys=True,
        )
        if key in seen and seen[key].get("value_str") != snap.get("value_str"):
            raise LedgerConflictError(
                "conflicting candidates for the same metric/run/kind; "
                "conflict_policy=fail refuses latest/best auto-selection"
            )
        seen.setdefault(key, snap)
    snapshots = _apply_unit_conversions(snapshots, query.unit_conversions)
    aggregate_rows: list[dict[str, Any]] = []
    for spec in query.aggregations:
        aggregate_rows.extend(_aggregate(snapshots, spec, purpose=query.purpose))
    # Detail rows for non-aggregated metrics (one row per snapshot, sorted).
    detail_rows = [
        {
            "row_id": f"detail-{s['evidence_snapshot_digest'][:16]}",
            "metric_name": s.get("metric_name"),
            "value_str": s.get("value_str"),
            "unit": s.get("unit"),
            "reducer": None,
            "sample_count": 1 if s.get("value_str") is not None else 0,
            "source_evidence_ids": [s.get("evidence_id")],
            "run_identity": s.get("run_identity"),
            "measurement_kind": s.get("measurement_kind"),
            "lineage": [
                {
                    "evidence_id": s.get("evidence_id"),
                    "measurement_kind": s.get("measurement_kind"),
                    "method": s.get("method"),
                    "method_version": s.get("method_version"),
                    "parent_evidence_ids": s.get("parent_evidence_ids"),
                    "evidence_snapshot_digest": s.get("evidence_snapshot_digest"),
                }
            ],
        }
        for s in sorted(snapshots, key=lambda s: str(s.get("evidence_snapshot_digest")))
    ]
    query_digest = query.digest()
    stable: dict[str, Any] = {
        "schema": LEDGER_SCHEMA,
        "query_digest": query_digest,
        "query": query.to_canonical(),
        "generator_version": GENERATOR_VERSION,
        "decimal_aggregation_version": DECIMAL_AGGREGATION_VERSION,
        "decimal_context": {"prec": DECIMAL_CONTEXT.prec, "rounding": str(DECIMAL_CONTEXT.rounding)},
        "std_algorithm": STD_ALGORITHM,
        "snapshots": sorted(snapshots, key=lambda s: str(s.get("evidence_snapshot_digest"))),
        "aggregate_rows": sorted(aggregate_rows, key=lambda r: str(r.get("row_id"))),
        "detail_rows": sorted(detail_rows, key=lambda r: str(r.get("row_id"))),
        "selection_policy": {
            "missing_policy": query.missing_policy,
            "conflict_policy": query.conflict_policy,
            "purpose": query.purpose,
        },
    }
    digest = hashlib.sha256(_canonical_json(stable)).hexdigest()
    ledger_id = f"ledger-{digest[:16]}"
    from .immutable_artifacts import write_immutable_bundle

    payload: dict[str, Any] = {
        **stable, "ledger_id": ledger_id, "ledger_digest": digest, "created_at": utc_now(),
        "diagnostic_marking": (
            "NOT PUBLICATION EVIDENCE" if query.purpose == "diagnostic"
            else "scientific snapshot; not claim verified"
        ),
    }
    write_immutable_bundle(root, {
        f".paperforge/ledgers/{ledger_id}/query.json": {"query": query.to_canonical(), "query_digest": query_digest},
        f".paperforge/ledgers/{ledger_id}/ledger.json": payload,
        f".paperforge/ledgers/{ledger_id}/manifest.json": {
            "ledger_id": ledger_id, "ledger_digest": digest, "generator_version": GENERATOR_VERSION,
            "decimal_aggregation_version": DECIMAL_AGGREGATION_VERSION, "query_digest": query_digest,
        },
    })
    payload = json.loads((root / ".paperforge/ledgers" / ledger_id / "ledger.json").read_bytes())
    _register_ledger_artifacts(root, memory, ledger_id, digest, query_digest, payload, run_id=run_id)
    return payload


def _register_ledger_artifacts(
    root: Path,
    memory: Any,
    ledger_id: str,
    digest: str,
    query_digest: str,
    payload: Mapping[str, Any],
    *,
    run_id: str | None = None,
) -> list[dict[str, Any]]:
    """Register ledger.json + manifest.json as formal artifacts (no second DB).

    Files stay the immutable snapshot; ScientificMemory/ArtifactStore is the
    authority index for U2.4/Claim Gate/Publication/Release.
    """
    from .artifacts import ArtifactStore

    snapshots = list(payload.get("snapshots") or [])
    ledger_path = root / ".paperforge" / "ledgers" / ledger_id / "ledger.json"
    file_sha = hashlib.sha256(ledger_path.read_bytes()).hexdigest() if ledger_path.is_file() else ""
    metadata = {
        "ledger_id": ledger_id,
        "ledger_digest": digest,
        "ledger_file_sha256": file_sha,
        "query_digest": query_digest,
        "generator_version": GENERATOR_VERSION,
        "decimal_aggregation_version": DECIMAL_AGGREGATION_VERSION,
        "purpose": str((payload.get("selection_policy") or {}).get("purpose") or ""),
        "evidence_snapshot_digests": sorted(
            str(s.get("evidence_snapshot_digest") or "") for s in snapshots if s.get("evidence_snapshot_digest")
        ),
        "workflow_id": run_id,
    }
    store = ArtifactStore(
        root,
        allowed_roots=(".paperforge/ledgers",),
        allowed_suffixes=(".json",),
        allowed_kinds=("result", "manifest", "report"),
        memory=memory,
    )
    from .immutable_artifacts import register_immutable

    records = [
        register_immutable(store,
            f".paperforge/ledgers/{ledger_id}/ledger.json",
            kind="result", run_id=run_id, status="VERIFIED",
            media_type="application/json", metadata=metadata,
        ),
        register_immutable(store,
            f".paperforge/ledgers/{ledger_id}/manifest.json",
            kind="manifest", run_id=run_id, status="VERIFIED",
            media_type="application/json", metadata=metadata,
        ),
    ]
    return [r if isinstance(r, dict) else r.to_dict() for r in records]


def _unregister_ledger_artifacts(root: Path, memory: Any, ledger_id: str) -> None:
    with memory.connect() as db:
        for rel in (
            f".paperforge/ledgers/{ledger_id}/ledger.json",
            f".paperforge/ledgers/{ledger_id}/manifest.json",
        ):
            with contextlib.suppress(OSError):
                db.execute("DELETE FROM artifacts WHERE path=?", (rel,))


def _ledger_paths(root: Path, ledger_id: str) -> tuple[Path, Path, Path]:
    base = root / ".paperforge" / "ledgers" / ledger_id
    return base / "query.json", base / "ledger.json", base / "manifest.json"


def verify_ledger(workspace: str | Path, memory: Any, ledger_id: str) -> dict[str, Any]:
    """Frozen files never change; report historical vs current validity separately."""
    from .external_import import approval_valid

    root = Path(workspace).expanduser().resolve(strict=True)
    _, ledger_path, _ = _ledger_paths(root, ledger_id)
    if not ledger_path.is_file():
        raise LedgerError(f"ledger missing: {ledger_id}")
    raw = ledger_path.read_bytes()
    file_sha = hashlib.sha256(raw).hexdigest()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        return {"ledger_id": ledger_id, "ledger_file_integrity": "failed", "detail": str(exc)}
    stored_digest = str(payload.get("ledger_digest") or "")
    stable_payload = {
        "schema": payload.get("schema"),
        "query_digest": payload.get("query_digest"),
        "query": payload.get("query"),
        "generator_version": payload.get("generator_version"),
        "decimal_aggregation_version": payload.get("decimal_aggregation_version"),
        "decimal_context": payload.get("decimal_context"),
        "std_algorithm": payload.get("std_algorithm"),
        "snapshots": payload.get("snapshots"),
        "aggregate_rows": payload.get("aggregate_rows"),
        "detail_rows": payload.get("detail_rows"),
        "selection_policy": payload.get("selection_policy"),
    }
    recomputed = hashlib.sha256(_canonical_json(stable_payload)).hexdigest()
    digest_valid = stored_digest == recomputed
    # Snapshot self-consistency over canonical bodies (lexical provenance excluded).
    snapshot_ok = True
    for snap in payload.get("snapshots", []) or []:
        body = {
            k: snap[k]
            for k in snap
            if k not in {"evidence_snapshot_digest", "source_lexical_value", "source_decimal_places"}
        }
        if hashlib.sha256(_canonical_json(body)).hexdigest() != str(snap.get("evidence_snapshot_digest") or ""):
            snapshot_ok = False
            break
    # Currentness: evidence still present + import approvals still valid.
    stale: list[str] = []
    with memory.connect() as db:
        for snap in payload.get("snapshots", []) or []:
            eid = str(snap.get("evidence_id") or "")
            if not eid:
                continue
            row = db.execute("SELECT * FROM evidence WHERE id=?", (eid,)).fetchone()
            if row is None:
                stale.append(eid)
                continue
            try:
                meta = json.loads(str(row["metadata_json"]))
            except (TypeError, ValueError):
                meta = {}
            import_id = meta.get("import_id") if isinstance(meta, dict) else None
            if snap.get("adapter_id") and (not import_id or import_id != snap.get("import_id")):
                stale.append(eid)
                continue
            if import_id:
                try:
                    valid, _ = approval_valid(workspace, memory, str(import_id))
                    if valid and snap.get("adapter_id"):
                        # Re-derive imported observations, not just the approval.
                        # This also checks current row provenance and semantics.
                        candidates = _imported_metric_snapshots(
                            root, memory, eid, dict(row), meta,
                            ResultLedgerQuery(purpose="diagnostic", evidence_ids=(eid,)))
                        valid = any(candidate["evidence_snapshot_digest"] == snap.get("evidence_snapshot_digest")
                                    for candidate in candidates)
                except Exception:
                    valid = False
                if not valid:
                    stale.append(eid)
    semantics_ok = True
    for snap in payload.get("snapshots", []) or []:
        try:
            EvidenceSemantics.from_metadata(
                {
                    "measurement_kind": snap.get("measurement_kind"),
                    "acquisition_origin": snap.get("acquisition_origin"),
                    "environment_role": snap.get("environment_role"),
                    "parent_evidence_ids": snap.get("parent_evidence_ids") or (),
                    "derivation_method": snap.get("method") or None,
                    "derivation_version": snap.get("method_version") or None,
                }
            )
        except Exception:
            # Lineage-light rows (e.g. aggregate parents) still require vocabularies.
            if str(snap.get("measurement_kind") or "") not in {
                "measured", "simulated", "remapped", "derived", "transcribed",
            }:
                semantics_ok = False
    historical = "valid" if (digest_valid and snapshot_ok) else "invalid"
    currentness = "current" if not stale else "stale"
    from .immutable_artifacts import verify_registered_bundle

    registry = verify_registered_bundle(root, memory, {
        f".paperforge/ledgers/{ledger_id}/ledger.json": "result",
        f".paperforge/ledgers/{ledger_id}/manifest.json": "manifest"})
    eligible = bool(historical == "valid" and currentness == "current" and semantics_ok
                    and registry["registered_complete"])
    return {
        "ledger_id": ledger_id,
        **registry,
        "ledger_file_sha256": file_sha,
        "ledger_file_integrity": "valid" if ledger_path.is_file() else "failed",
        "ledger_digest_valid": digest_valid,
        "source_snapshot_integrity": "valid" if snapshot_ok else "invalid",
        "source_currentness": currentness,
        "stale_evidence_ids": sorted(set(stale)),
        "current_approval_validity": "valid" if not stale else "stale_or_invalid",
        "scientific_semantics_valid": semantics_ok,
        "historical_integrity": historical,
        # U2.3A validates ledger integrity/currentness/semantics only. It does
        # not imply Claim Gate, scientific review, or publication readiness.
        "eligible_as_release_input": eligible,
        "currently_usable_for_scientific_release": eligible,  # legacy alias
        "warnings": [] if historical == "valid" else ["ledger file or digest mismatch"],
    }


__all__ = [
    "CROSSABLE_DIMENSIONS",
    "DECIMAL_AGGREGATION_VERSION",
    "DECIMAL_CONTEXT",
    "GENERATOR_VERSION",
    "LEDGER_SCHEMA",
    "QUERY_SCHEMA",
    "STD_ALGORITHM",
    "AggregationSpec",
    "LedgerConflictError",
    "LedgerError",
    "ResultLedgerQuery",
    "freeze_ledger",
    "verify_ledger",
]
