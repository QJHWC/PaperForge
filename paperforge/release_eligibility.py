from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .models import utc_now
from .immutable_artifacts import register_immutable, verify_registered_bundle, write_immutable_bundle
from .path_safety import atomic_write_text, reject_symlink_components, safe_mkdir

REPORT_SCHEMA = "paperforge.release-eligibility-report/v1"
POLICY_SCHEMA = "paperforge.release-policy/v1"
POLICY_VERSION = "paperforge.release-policy/v1"

CATEGORIES = (
    "source_integrity",
    "scientific_evidence",
    "scientific_review",
    "reviewer_resolution",
    "pdf_layout_qa",
    "human_approval",
    "engineering_quality",
    "environment_readiness",
)


class ReleaseEligibilityError(RuntimeError):
    pass


class _ReadOnlyMemory:
    """Prevent legacy verification helpers from creating or changing DB state."""

    def __init__(self, memory: Any):
        self.memory = memory

    @contextmanager
    def connect(self):
        with self.memory.connect() as db:
            db.execute("PRAGMA query_only = ON")
            yield db


@dataclass(frozen=True)
class ReleaseTarget:
    target_id: str
    manuscript_path: str = ""
    manuscript_sha256: str = ""
    final_pdf_path: str = ""
    final_pdf_sha256: str = ""
    bibliography_hashes: Mapping[str, Any] = field(default_factory=dict)
    build_digest: str = ""
    build_manifest_path: str = ""
    workflow_id: str = ""
    baseline_id: str = ""
    created_at: str = ""

    def __post_init__(self) -> None:
        if not str(self.target_id or "").strip():
            raise ReleaseEligibilityError("target_id is required")
        object.__setattr__(self, "bibliography_hashes", dict(self.bibliography_hashes or {}))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["bibliography_hashes"] = dict(payload["bibliography_hashes"])
        return payload


@dataclass(frozen=True)
class ReleasePolicy:
    policy_id: str = "default"
    version: str = POLICY_VERSION
    required_gates: tuple[str, ...] = CATEGORIES
    allowed_warning_classes: tuple[str, ...] = ()
    require_human_owner_ack: bool = False
    engineering_blocking: bool = True
    strict_reviewer_verification: bool = True
    allowed_review_states: tuple[str, ...] = ("reviewed",)

    def __post_init__(self) -> None:
        unknown = [g for g in (self.required_gates or ()) if g not in CATEGORIES]
        if unknown:
            raise ReleaseEligibilityError(f"unknown gate categories: {unknown}")
        object.__setattr__(self, "required_gates", tuple(self.required_gates or ()))
        object.__setattr__(self, "allowed_warning_classes", tuple(self.allowed_warning_classes or ()))
        object.__setattr__(self, "allowed_review_states", tuple(self.allowed_review_states))

    def to_canonical(self) -> dict[str, Any]:
        return {"schema": POLICY_SCHEMA, "policy_id": self.policy_id, "version": self.version,
                "required_gates": list(self.required_gates),
                "allowed_warning_classes": list(self.allowed_warning_classes),
                "require_human_owner_ack": self.require_human_owner_ack,
                "engineering_blocking": self.engineering_blocking,
                "strict_reviewer_verification": self.strict_reviewer_verification,
                "allowed_review_states": list(self.allowed_review_states)}

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(
            self.to_canonical(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class ReleaseGateResult:
    gate_id: str
    category: str
    requirement: str
    status: str
    severity: str = "major"
    source_artifact_ids: tuple[str, ...] = ()
    source_digests: tuple[str, ...] = ()
    currentness: str = "current"
    rationale: str = ""
    remediation: str = ""
    policy_rule: str = ""

    def __post_init__(self) -> None:
        if self.category not in CATEGORIES:
            raise ReleaseEligibilityError(f"unknown category: {self.category}")
        if self.status not in {"passed", "failed", "needs_human", "blocked", "not_applicable"}:
            raise ReleaseEligibilityError(f"unknown gate status: {self.status}")
        object.__setattr__(self, "source_artifact_ids", tuple(self.source_artifact_ids or ()))
        object.__setattr__(self, "source_digests", tuple(self.source_digests or ()))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["source_artifact_ids"] = list(payload["source_artifact_ids"])
        payload["source_digests"] = list(payload["source_digests"])
        return payload


@dataclass(frozen=True)
class EngineeringCheckResult:
    check_id: str
    command: str = ""
    status: str = "not_run"
    exit_code: int | None = None
    collected: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: int = 0
    environment_findings: tuple[str, ...] = ()
    log_artifact: str = ""
    verifier: str = ""
    currentness: str = "current"
    category: str = "engineering_quality"
    record_path: str = ""

    def __post_init__(self) -> None:
        if self.status not in {"passed", "failed", "unavailable", "not_run", "blocked_environment"}:
            raise ReleaseEligibilityError(f"unknown engineering status: {self.status}")
        if self.category not in {"engineering_quality", "environment_readiness"}:
            raise ReleaseEligibilityError("invalid engineering category")
        object.__setattr__(self, "environment_findings", tuple(self.environment_findings or ()))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["environment_findings"] = list(payload["environment_findings"])
        return payload


def _canonical(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload)).hexdigest()


def _path(root: Path, relative: str) -> Path:
    path = Path(relative)
    if (not relative or path.is_absolute() or ".." in path.parts
            or any(c in relative for c in "*?[]\\") or path.as_posix() != relative):
        raise ReleaseEligibilityError("expected an exact workspace-relative path")
    candidate = reject_symlink_components(root / path, anchor=root)
    if not candidate.is_file() or not candidate.resolve().is_relative_to(root):
        raise ReleaseEligibilityError("bound file missing or unsafe")
    return candidate


def _json_file(root: Path, relative: str) -> dict[str, Any]:
    data = json.loads(_path(root, relative).read_bytes())
    if not isinstance(data, dict):
        raise ReleaseEligibilityError("expected a JSON object")
    return data


def _identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ReleaseEligibilityError("invalid artifact identifier")
    return value


def _file_hash(root: Path, relative: str) -> str:
    try:
        return hashlib.sha256(_path(root, relative).read_bytes()).hexdigest()
    except (OSError, ValueError, ReleaseEligibilityError):
        return "missing_or_unsafe"


def _target_binding(target: ReleaseTarget) -> dict[str, Any]:
    return {k: v for k, v in target.to_dict().items() if k != "created_at"}


def _validate_import_files(root: Path, import_id: str) -> None:
    base = f".paperforge/imports/{_identifier(import_id)}"
    manifest = _json_file(root, f"{base}/manifest.json")
    _path(root, f"{base}/validation.json")
    for source in manifest.get("sources") or []:
        _path(root, f"{base}/{source['path']}")


def _validate_ledger_files(root: Path, memory: Any, ledger_id: str) -> dict[str, Any]:
    ledger = _json_file(root, f".paperforge/ledgers/{_identifier(ledger_id)}/ledger.json")
    with memory.connect() as db:
        for snapshot in ledger.get("snapshots") or []:
            row = db.execute("SELECT metadata_json FROM evidence WHERE id=?",
                             (snapshot.get("evidence_id"),)).fetchone()
            if row:
                meta = json.loads(row["metadata_json"])
                if meta.get("import_id"):
                    _validate_import_files(root, str(meta["import_id"]))
    return ledger


def _build_valid(root: Path, target: ReleaseTarget) -> bool:
    try:
        if _file_hash(root, target.build_manifest_path) != target.build_digest:
            return False
        build = _json_file(root, target.build_manifest_path)
        sources = build.get("source_hashes")
        expected = {**target.bibliography_hashes, target.manuscript_path: target.manuscript_sha256}
        return bool(
            build.get("schema") == "paperforge.build-provenance/v1"
            and build.get("status") == "succeeded"
            and build.get("command") and build.get("toolchain")
            and isinstance(sources, dict) and sources
            and all(sources.get(p) == sha for p, sha in expected.items())
            and all(_file_hash(root, p) == sha for p, sha in sources.items())
            and build.get("output_path") == target.final_pdf_path
        and build.get("output_sha256") == target.final_pdf_sha256
            and (not target.workflow_id or build.get("workflow_id") == target.workflow_id)
            and (not target.baseline_id or build.get("baseline_id") == target.baseline_id)
        )
    except (OSError, ValueError, TypeError, ReleaseEligibilityError):
        return False


def _snapshot(root: Path, memory: Any, target: ReleaseTarget,
              inputs: Mapping[str, Any]) -> dict[str, Any]:
    paths = {target.manuscript_path, target.final_pdf_path, target.build_manifest_path,
             *target.bibliography_hashes}
    try:
        paths.update(_json_file(root, target.build_manifest_path).get("source_hashes", {}))
    except (OSError, ValueError, TypeError, ReleaseEligibilityError):
        pass
    for key, folder, name in (("ledger_ids", "ledgers", "ledger.json"),
                               ("review_ids", "reviews", "review.json"),
                               ("pdf_bundle_ids", "pdf_qa", "qa.json")):
        for item in inputs.get(key) or []:
            paths.add(f".paperforge/{folder}/{_identifier(str(item))}/{name}")
    matrix_id = (inputs.get("reviewer_projection") or {}).get("matrix_id")
    if matrix_id:
        paths.add(f".paperforge/review_matrix/{_identifier(str(matrix_id))}/matrix.json")
    for check in inputs.get("engineering_checks") or []:
        if check.get("log_artifact"):
            paths.add(check["log_artifact"])
        if check.get("record_path"):
            paths.add(check["record_path"])
    for review_id in inputs.get("review_ids") or []:
        try:
            review = _json_file(root, f".paperforge/reviews/{_identifier(review_id)}/review.json")
            paths.update(review.get("fact_artifact_hashes") or {})
        except (OSError, ValueError, TypeError, ReleaseEligibilityError):
            pass
    # Pin mutable scientific/approval state as well as frozen file bytes.
    # Conservative invalidation on unrelated evidence changes is intentional.
    state = {}
    with memory.connect() as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for name in ("evidence", "imports", "import_approvals", "result_updates",
                     "result_approvals", "result_baselines"):
            if name in tables:
                state[name] = sorted((dict(r) for r in db.execute(f"SELECT * FROM {name}")),
                                     key=lambda row: json.dumps(row, sort_keys=True))
    for row in state.get("evidence", []):
        try:
            meta = json.loads(row["metadata_json"])
            if meta.get("import_id"):
                base = f".paperforge/imports/{_identifier(str(meta['import_id']))}"
                paths.update((f"{base}/manifest.json", f"{base}/validation.json"))
                manifest = _json_file(root, f"{base}/manifest.json")
                paths.update(f"{base}/{s['path']}" for s in manifest.get("sources") or [])
        except (OSError, ValueError, TypeError, KeyError, ReleaseEligibilityError):
            continue
    pending_paths = list(paths)
    visited = set()
    while pending_paths:
        path = pending_paths.pop()
        if path in visited or not path.endswith(".json"):
            continue
        visited.add(path)
        try:
            bundle = _json_file(root, path)
            linked = set(bundle.get("fact_artifact_hashes") or {})
            linked.update(f".paperforge/ledgers/{_identifier(str(b['ledger_id']))}/ledger.json"
                          for b in bundle.get("ledger_bindings") or [])
            linked.update(f".paperforge/reviews/{_identifier(str(r))}/review.json"
                          for r in bundle.get("review_ids") or [])
            for binding in bundle.get("bindings") or []:
                locations = {"ledger": ("ledgers", "ledger.json"),
                             "review_bundle": ("reviews", "review.json"),
                             "pdf_qa": ("pdf_qa", "qa.json")}
                if binding.get("artifact_kind") in locations:
                    folder, name = locations[binding["artifact_kind"]]
                    linked.add(f".paperforge/{folder}/{_identifier(binding['artifact_id'])}/{name}")
            paths.update(linked)
            pending_paths.extend(linked - visited)
        except (OSError, ValueError, TypeError, KeyError, ReleaseEligibilityError):
            continue
    return {"file_hashes": {p: _file_hash(root, p) for p in sorted(paths) if p},
            "domain_digest": _digest(state)}


def _gate(gate_id: str, category: str, requirement: str, status: str, rationale: str = "",
          remediation: str = "", severity: str = "major",
          artifacts: Sequence[str] = (), digests: Sequence[str] = (),
          currentness: str = "current", rule: str = "") -> ReleaseGateResult:
    return ReleaseGateResult(
        gate_id=gate_id, category=category, requirement=requirement, status=status,
        severity=severity, source_artifact_ids=tuple(artifacts), source_digests=tuple(digests),
        currentness=currentness, rationale=rationale, remediation=remediation, policy_rule=rule)


def evaluate_release(
    workspace: str | Path,
    memory: Any,
    target: ReleaseTarget,
    policy: ReleasePolicy,
    *,
    evidence_refs: Sequence[Mapping[str, Any]] | None = None,
    ledger_ids: Sequence[str] | None = None,
    review_ids: Sequence[str] | None = None,
    reviewer_projection: Mapping[str, Any] | None = None,
    pdf_bundle_ids: Sequence[str] | None = None,
    human_records: Sequence[Mapping[str, Any]] | None = None,
    engineering_checks: Sequence[EngineeringCheckResult | Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Read-only eligibility projection. Never mutates Evidence, reviews, or files."""
    root = Path(workspace).expanduser().resolve(strict=True)
    memory = _ReadOnlyMemory(memory)
    gates: list[ReleaseGateResult] = []
    inputs = {
        "evidence_refs": [dict(e) for e in evidence_refs or ()],
        "ledger_ids": list(ledger_ids or ()), "review_ids": list(review_ids or ()),
        "reviewer_projection": dict(reviewer_projection or {}),
        "pdf_bundle_ids": list(pdf_bundle_ids or ()),
        "human_records": [dict(h) for h in human_records or ()],
        "engineering_checks": [c.to_dict() if isinstance(c, EngineeringCheckResult) else dict(c)
                               for c in engineering_checks or ()],
    }

    # A. source integrity: exact byte binding, no latest/glob.
    try:
        manuscript = _path(root, target.manuscript_path).read_bytes()
        manuscript_ok = bool(manuscript) and hashlib.sha256(manuscript).hexdigest() == target.manuscript_sha256
    except (OSError, ValueError, ReleaseEligibilityError):
        manuscript_ok = False
    try:
        pdf = _path(root, target.final_pdf_path).read_bytes()
        pdf_ok = bool(pdf) and hashlib.sha256(pdf).hexdigest() == target.final_pdf_sha256
    except (OSError, ValueError, ReleaseEligibilityError):
        pdf_ok = False
    gates.append(_gate("source.manuscript", "source_integrity", "manuscript bytes match target",
                       "passed" if manuscript_ok else "failed",
                       rationale="manuscript SHA binding"))
    gates.append(_gate("source.pdf", "source_integrity", "final PDF bytes match target",
                       "passed" if pdf_ok else "failed",
                       rationale="PDF SHA binding"))
    gates.append(_gate("source.build", "source_integrity", "build binds all current inputs and output",
                       "passed" if _build_valid(root, target) else "blocked",
                       rationale="exact build manifest bytes and source/PDF bindings required",
                       artifacts=(target.build_manifest_path,), digests=(target.build_digest,)))
    for path, sha in target.bibliography_hashes.items():
        gates.append(_gate(f"source.bibliography.{path}", "source_integrity", "bibliography current",
                           "passed" if _file_hash(root, path) == sha else "failed"))
    if target.baseline_id:
        valid_baseline = False
        try:
            with memory.connect() as db:
                baseline = db.execute("SELECT * FROM result_baselines WHERE target_path=?",
                                      (target.manuscript_path,)).fetchone()
                update = db.execute("SELECT * FROM result_updates WHERE id=?",
                                    (target.baseline_id,)).fetchone()
                approval = db.execute("SELECT * FROM result_approvals WHERE id=?",
                                      (baseline["approval_id"] if baseline else "",)).fetchone()
            valid_baseline = bool(baseline and update and approval
                                  and baseline["proposal_id"] == target.baseline_id
                                  and baseline["source_sha256"] == target.manuscript_sha256
                                  and update["state"] == "APPLIED"
                                  and approval["proposal_id"] == target.baseline_id
                                  and approval["proposal_digest"] == update["proposal_digest"]
                                  and approval["status"] == "valid")
        except Exception:
            valid_baseline = False
        gates.append(_gate("source.baseline", "source_integrity", "result baseline and approval current",
                           "passed" if valid_baseline else "blocked"))

    # B. scientific evidence: current, non-fixture, non-transcribed-only.
    evidence_refs = list(evidence_refs or [])
    resolved_evidence = []
    missing_evidence = []
    with memory.connect() as db:
        for ref in evidence_refs:
            eid = str(ref.get("evidence_id") or "")
            row = db.execute("SELECT * FROM evidence WHERE id=?", (eid,)).fetchone()
            if row is None:
                missing_evidence.append(eid)
                continue
            meta = json.loads(row["metadata_json"])
            if meta.get("import_id"):
                try:
                    from .external_import import approval_valid

                    import_id = _identifier(str(meta["import_id"]))
                    _validate_import_files(root, import_id)
                    valid, _ = approval_valid(root, memory, import_id)
                    meta["current"] = valid
                except Exception:
                    meta["current"] = False
            resolved_evidence.append({**meta, "evidence_id": eid})
    if missing_evidence:
        gates.append(_gate("evidence.missing", "scientific_evidence", "evidence exists in memory",
                           "blocked", rationale="requested evidence missing"))
    evidence_refs = resolved_evidence
    if not evidence_refs:
        gates.append(_gate("evidence.required", "scientific_evidence", "required scientific inputs current",
                           "blocked", rationale="missing required evidence (never N/A)",
                           remediation="materialize required evidence"))
    else:
        blocked = [str(e.get("evidence_id")) for e in evidence_refs
                   if str((e.get("environment_role") or "")) != "scientific"
                   or str(e.get("measurement_kind") or "") not in {"measured", "simulated", "remapped", "derived"}]
        stale = [str(e.get("evidence_id")) for e in evidence_refs if e.get("current") is False]
        if blocked:
            gates.append(_gate("evidence.semantics", "scientific_evidence", "no fixture/transcribed-only",
                               "blocked", rationale=f"inadmissible: {blocked[:3]}"))
        elif stale:
            gates.append(_gate("evidence.currentness", "scientific_evidence", "inputs current",
                               "blocked", rationale=f"stale: {stale[:3]}"))
        else:
            gates.append(_gate("evidence.current", "scientific_evidence", "inputs current",
                               "passed", rationale="evidence refs current"))
    for ledger_id in ledger_ids or ():
        try:
            from .result_ledger import verify_ledger

            ledger = _validate_ledger_files(root, memory, ledger_id)
            verification = verify_ledger(root, memory, ledger_id)
            snapshots = ledger.get("snapshots") or []
            valid = bool(verification.get("eligible_as_release_input") and snapshots and all(
                s.get("environment_role") == "scientific" and s.get("measurement_kind") != "transcribed"
                for s in snapshots))
            gates.append(_gate(f"ledger.{ledger_id}", "scientific_evidence", "ledger current and scientific",
                               "passed" if valid else "blocked"))
        except Exception:
            gates.append(_gate(f"ledger.{ledger_id}", "scientific_evidence", "ledger current", "blocked"))

    # C. scientific review: historical + current, reviewed vs caveats explicit.
    review_ids = list(review_ids or [])
    if not review_ids:
        gates.append(_gate("review.required", "scientific_review", "review current",
                           "blocked", rationale="no review bound"))
    else:
        try:
            from .scientific_review import verify_scientific_review

            problems = []
            for review_id in review_ids:
                bundle = _json_file(root, f".paperforge/reviews/{_identifier(review_id)}/review.json")
                for binding in bundle.get("ledger_bindings") or []:
                    _validate_ledger_files(root, memory, str(binding["ledger_id"]))
                report = verify_scientific_review(root, memory, str(review_id))
                if not report.get("currently_valid_as_review_input"):
                    problems.append(str(review_id))
                from .scientific_review import Assumption, aggregate_review_state

                assumptions = [Assumption(**a) for a in bundle.get("assumptions") or []]
                relevance = {str(c): [a.assumption_id for a in assumptions]
                             for c in bundle.get("coverage") or {}}
                review_state = aggregate_review_state(bundle.get("coverage") or {}, assumptions, relevance)
                if review_state not in policy.allowed_review_states:
                    problems.append(f"{review_id}: state not allowed")
                for path, sha in (bundle.get("fact_artifact_hashes") or {}).items():
                    if _file_hash(root, path) != sha:
                        problems.append(f"{review_id}: source changed")
            gates.append(_gate(
                "review.current", "scientific_review", "reviews currently valid",
                "passed" if not problems else "blocked",
                rationale="stale reviews never satisfy current gate" if problems else "reviews current"))
        except Exception as exc:
            gates.append(_gate("review.current", "scientific_review", "reviews currently valid",
                               "blocked", rationale=f"review check failed: {type(exc).__name__}"))

    # D. reviewer resolution: required criteria only; prose never counts.
    projection = dict(reviewer_projection or {})
    required_states = []
    try:
        matrix_id = _identifier(str(projection.get("matrix_id") or ""))
        matrix = _json_file(root, f".paperforge/review_matrix/{matrix_id}/matrix.json")
        stable_matrix = {k: v for k, v in matrix.items()
                         if k not in {"matrix_id", "matrix_digest", "created_at", "matrix_file_sha256"}}
        comments_valid = all(c.get("comment_digest") == hashlib.sha256(c.get("exact_text", "").encode()).hexdigest()
                             for c in matrix.get("comments") or [])
        matrix_registry = verify_registered_bundle(root, memory, {
            f".paperforge/review_matrix/{matrix_id}/matrix.json": "review",
            f".paperforge/review_matrix/{matrix_id}/manifest.json": "manifest"})
        # The legacy matrix verifier visits optional bindings and may reconcile.
        # Verify history here, then inspect only required bindings without repair.
        if (_digest(stable_matrix) != matrix.get("matrix_digest") or not comments_valid
                or not matrix_registry["registered_complete"]):
            required_states = ["blocked"]
        else:
            required_ids = {c["criterion_id"] for c in matrix.get("criteria", []) if c.get("required")}
            required_actions = [a for a in matrix.get("actions", [])
                                if required_ids.intersection(a.get("acceptance_criteria") or [])]
            required_states = [str(a.get("status")) for a in required_actions]
            referenced = {c for a in required_actions for c in a.get("acceptance_criteria") or []}
            if not required_ids.issubset(referenced):
                required_states.append("blocked")
            for action in required_actions:
                bindings = [b for b in matrix.get("bindings", []) if b.get("action_id") == action.get("action_id")]
                satisfied = set()
                for binding in bindings:
                    meta = binding.get("metadata") or {}
                    if not binding.get("expected_digest"):
                        continue
                    if binding.get("artifact_kind") == "human_verification":
                        bound = (meta.get("target_digest") == _digest(_target_binding(target))
                                 and meta.get("verifier") and meta.get("verified_at")
                                 and meta.get("rationale") and meta.get("verification_source") == "human_explicit"
                                 and binding["expected_digest"] == _digest(meta))
                    elif binding.get("artifact_kind") == "imported_evidence":
                        eid = str(binding.get("artifact_id") or "")
                        bound = any(e.get("evidence_id") == eid and e.get("current") is not False
                                    and e.get("environment_role") == "scientific"
                                    and e.get("measurement_kind") in {"measured", "simulated", "remapped", "derived"}
                                    for e in evidence_refs)
                        with memory.connect() as db:
                            row = db.execute("SELECT * FROM evidence WHERE id=?", (eid,)).fetchone()
                        bound = bool(bound and row and binding["expected_digest"] == row["content_sha256"])
                    else:
                        # Until a binding kind has a hash-aware release adapter,
                        # presence-only legacy verification cannot authorize it.
                        bound = False
                    if bound:
                        for criterion in matrix.get("criteria") or []:
                            cid = criterion["criterion_id"]
                            if cid not in (meta.get("satisfies") or []):
                                continue
                            allowed_bindings = {
                                "hardware_measurement": {"imported_evidence"},
                                "scientific_experiment": {"imported_evidence"},
                                "layout": {"human_verification"},
                                "manuscript_clarification": {"human_verification"},
                            }
                            if binding.get("artifact_kind") not in allowed_bindings.get(
                                    criterion.get("criterion_type"), set()):
                                continue
                            if criterion.get("criterion_type") in {"hardware_measurement", "scientific_experiment"}:
                                if binding.get("artifact_kind") != "imported_evidence":
                                    continue
                                if criterion.get("criterion_type") == "hardware_measurement" and not any(
                                        e.get("evidence_id") == binding.get("artifact_id")
                                        and e.get("measurement_kind") == "measured" for e in evidence_refs):
                                    continue
                            satisfied.add(cid)
                if not required_ids.intersection(action.get("acceptance_criteria") or []).issubset(satisfied):
                    required_states.append("blocked")
            if not required_ids:
                required_states = ["verified"]
    except Exception:
        required_states = ["blocked"]
    if not required_states:
        gates.append(_gate("reviewer.required", "reviewer_resolution", "required criteria verified",
                           "blocked", rationale="no required reviewer states bound"))
    elif all(s == "verified" for s in required_states):
        gates.append(_gate("reviewer.required", "reviewer_resolution", "required criteria verified",
                           "passed", rationale="required reviewer items verified"))
    elif any(s in {"blocked", "partial"} for s in required_states):
        gates.append(_gate("reviewer.required", "reviewer_resolution", "required criteria verified",
                           "blocked", rationale="required reviewer items blocked/partial"))
    else:
        strict = bool(policy.strict_reviewer_verification)
        gates.append(_gate("reviewer.required", "reviewer_resolution", "required criteria verified",
                           "blocked" if strict else "needs_human",
                           rationale="addressed-but-not-verified under strict policy"))

    # E. PDF QA: same SHA, current bundle, coverage, critical, target issues, human.
    pdf_bundle_ids = list(pdf_bundle_ids or [])
    if not pdf_bundle_ids:
        gates.append(_gate("pdfqa.required", "pdf_layout_qa", "PDF QA current for target SHA",
                           "blocked", rationale="no PDF QA bundle bound"))
    else:
        try:
            from .pdf_qa import verify_pdf_qa

            problems = []
            pending = []
            for bundle_id in pdf_bundle_ids:
                bundle = _json_file(root, f".paperforge/pdf_qa/{_identifier(bundle_id)}/qa.json")
                report = verify_pdf_qa(root, memory, str(bundle_id))
                # Keep the producer's canonical contract and registry integrity
                # separate: a complete file without registration is not current.
                stable_qa = {k: v for k, v in bundle.items()
                             if k not in {"bundle_id", "qa_digest", "created_at", "qa_file_sha256"}}
                if _digest(stable_qa) != bundle.get("qa_digest") or not report.get("registered_complete"):
                    problems.append(f"{bundle_id}: bundle invalid")
                    continue
                if (bundle.get("target_pdf_sha256") != target.final_pdf_sha256
                        or bundle.get("build_digest") != target.build_digest
                        or _file_hash(root, target.final_pdf_path) != target.final_pdf_sha256
                        or not _build_valid(root, target)):
                    problems.append(f"{bundle_id}: target/build mismatch")
                if report.get("layout_qa_state") == "needs_human":
                    pending.append(bundle_id)
                elif report.get("layout_qa_state") not in {"passed", "passed_with_warnings"}:
                    problems.append(f"{bundle_id}: state={report.get('layout_qa_state')}")
                if report.get("unresolved_critical"):
                    problems.append(f"{bundle_id}: unresolved critical")
                issues = bundle.get("layout_issues") or []
                for issue in issues:
                    if issue.get("status") in {"open", "needs_human"}:
                        if issue.get("severity") == "critical" or issue.get("target_specific"):
                            problems.append(f"{bundle_id}: unresolved required layout issue")
                        elif issue.get("status") == "needs_human":
                            pending.append(bundle_id)
                        elif issue.get("category") not in policy.allowed_warning_classes:
                            problems.append(f"{bundle_id}: warning not allowed")
                if report.get("layout_qa_state") == "passed_with_warnings" and not policy.allowed_warning_classes:
                    problems.append(f"{bundle_id}: warnings require explicit policy")
                renders = bundle.get("render_manifest") or []
                pages = {r.get("page"): r for r in renders}
                count = (bundle.get("preflight") or {}).get("page_count", 0)
                if not count or set(pages) != set(range(1, count + 1)) or any(
                        r.get("environment_role") != "production" for r in renders):
                    problems.append(f"{bundle_id}: incomplete or synthetic rendering")
                human = bundle.get("human_reviews") or []
                for page, render in pages.items():
                    matches = [h for h in human if h.get("page") == page
                               and h.get("pdf_sha256") == target.final_pdf_sha256
                               and h.get("render_sha256") == render.get("image_hash")
                               and h.get("environment_role") == "production"
                               and h.get("review_source") == "human_explicit"
                               and h.get("reviewer") and h.get("reviewed_at") and not h.get("stale")]
                    if not matches or not all(h.get("status") in {"passed", "passed_with_warnings"} for h in matches):
                        pending.append(bundle_id)
            gates.append(_gate("pdfqa.current", "pdf_layout_qa", "PDF QA current",
                               "blocked" if problems else "needs_human" if pending else "passed",
                               rationale="; ".join(problems[:3]) if problems else "PDF QA current"))
        except Exception as exc:
            gates.append(_gate("pdfqa.current", "pdf_layout_qa", "PDF QA current",
                               "blocked", rationale=f"PDF QA check failed: {type(exc).__name__}"))

    # F. human gates: explicit records only; the evaluator never mints approval.
    human_records = list(human_records or [])
    pdf_gate = next(g for g in gates if g.category == "pdf_layout_qa")
    gates.append(_gate("human.pdf_review", "human_approval", "existing target-bound PDF human reviews",
                       pdf_gate.status, rationale="consume existing reviews; owner acknowledgement is separate authorization"))

    # G/H. engineering + environment: unavailable/not_run never auto-pass.
    engineering_checks = list(engineering_checks or [])
    if not engineering_checks:
        gates.append(_gate("engineering.required", "engineering_quality", "engineering checks recorded",
                           "blocked", rationale="no engineering checks bound"))
    else:
        for check in engineering_checks:
            data = check.to_dict() if isinstance(check, EngineeringCheckResult) else dict(check)
            status = str(data.get("status") or "not_run")
            check_id = str(data.get("check_id") or data.get("command") or "check")
            category = str(data.get("category") or "engineering_quality")
            if category not in {"engineering_quality", "environment_readiness"}:
                category = "engineering_quality"
            if status == "passed":
                valid_check = False
                try:
                    record = _json_file(root, str(data.get("record_path") or ""))
                    valid_check = bool(
                        record.get("schema") == "paperforge.engineering-check/v1"
                        and record.get("target_digest") == _digest(_target_binding(target))
                        and record.get("result") == data
                        and data.get("currentness") == "current" and data.get("exit_code") == 0
                        and data.get("failed", 0) == 0 and data.get("errors", 0) == 0
                        and data.get("verifier") and data.get("command")
                        and not data.get("environment_findings")
                        and record.get("log_sha256") == _file_hash(root, str(data.get("log_artifact") or ""))
                        and re.fullmatch(r"[0-9a-f]{64}", str(record.get("log_sha256") or ""))
                    )
                except (OSError, ValueError, TypeError, ReleaseEligibilityError):
                    pass
                gates.append(_gate(f"engineering.{check_id}", category,
                                   f"{check_id} passed", "passed" if valid_check else "blocked",
                                   rationale="current target-bound result and log required"))
            elif status in {"unavailable", "not_run"}:
                gates.append(_gate(f"engineering.{check_id}", category,
                                   f"{check_id} recorded",
                                   "blocked" if policy.engineering_blocking else "needs_human",
                                   rationale=f"{status}: environment finding, never auto-pass"))
            elif status == "blocked_environment":
                gates.append(_gate(f"engineering.{check_id}", "environment_readiness",
                                   f"{check_id} environment", "blocked",
                                   rationale="environment-blocked is distinct from code failure"))
            else:
                gates.append(_gate(f"engineering.{check_id}", category,
                                   f"{check_id} recorded", "failed",
                                   rationale=f"status={status}"))

    # Aggregation: failed/blocked required -> NOT_ELIGIBLE; needs_human -> NEEDS_HUMAN.
    required = set(policy.required_gates)
    relevant = [g for g in gates if g.category in required]
    for category in required - {g.category for g in relevant}:
        missing = _gate(f"{category}.required", category, "required gate must be present", "blocked")
        gates.append(missing)
        relevant.append(missing)
    blocking = [g for g in relevant if g.status in {"failed", "blocked"}]
    human = [g for g in relevant if g.status == "needs_human"]
    if blocking:
        overall = "NOT_ELIGIBLE"
    elif human:
        overall = "NEEDS_HUMAN"
    else:
        overall = "ELIGIBLE"
    stable = {
        "schema": REPORT_SCHEMA,
        "target": _target_binding(target),
        "policy": policy.to_canonical(),
        "policy_digest": policy.digest(),
        "gates": sorted([g.to_dict() for g in gates], key=lambda g: str(g["gate_id"])),
        "blocking_gates": sorted(g.gate_id for g in blocking),
        "needs_human_gates": sorted(g.gate_id for g in human),
        "overall_eligibility": overall,
        "assessment_inputs": inputs,
        "currentness_snapshot": _snapshot(root, memory, target, inputs),
    }
    digest = hashlib.sha256(_canonical(stable)).hexdigest()
    report_id = f"release-{digest[:16]}"
    payload = {**stable, "report_id": report_id, "report_digest": digest, "created_at": utc_now()}
    return payload


def freeze_assessment(workspace: str | Path, memory: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Explicit audit persistence, separate from the read-only evaluator."""
    root = Path(workspace).expanduser().resolve(strict=True)
    report_id = _identifier(str(payload["report_id"]))
    stable = {k: v for k, v in payload.items() if k not in {"report_id", "report_digest", "created_at"}}
    digest = _digest(stable)
    if digest != payload.get("report_digest") or report_id != f"release-{digest[:16]}":
        raise ReleaseEligibilityError("assessment digest mismatch")
    target = ReleaseTarget(**payload["target"])
    policy = ReleasePolicy(**{k: v for k, v in payload["policy"].items() if k != "schema"})
    current = evaluate_release(root, memory, target, policy, **payload["assessment_inputs"])
    if current["report_digest"] != digest:
        raise ReleaseEligibilityError("assessment changed before freeze")
    report_dir = safe_mkdir(root / ".paperforge" / "release" / report_id, anchor=root)
    existing = report_dir / "report.json"
    if existing.exists():
        previous = _json_file(root, existing.relative_to(root).as_posix())
        if {k: v for k, v in previous.items() if k != "created_at"} != {
                k: v for k, v in payload.items() if k != "created_at"}:
            raise ReleaseEligibilityError("frozen assessment collision")
        payload = previous
    files = {
        f".paperforge/release/{report_id}/{name}": content for name, content in (
        ("target.json", {"target": target.to_dict()}),
        ("report.json", payload),
        ("manifest.json", {"report_id": report_id, "report_digest": digest,
                           "target_id": target.target_id, "policy_digest": policy.digest()}),
    )}
    write_immutable_bundle(root, files)
    from .artifacts import ArtifactStore

    store = ArtifactStore(root, allowed_roots=(".paperforge/release",),
                          allowed_suffixes=(".json",),
                          allowed_kinds=("release", "manifest", "report"), memory=memory)
    metadata = {"report_id": report_id, "report_digest": digest, "target_id": target.target_id,
                "policy_digest": policy.digest(), "overall_eligibility": payload["overall_eligibility"],
                "note": "release assessment, not scientific evidence"}
    for name, kind in (("report.json", "release"), ("manifest.json", "manifest")):
        register_immutable(store, f".paperforge/release/{report_id}/{name}", kind=kind,
                       status="VERIFIED", media_type="application/json", metadata=metadata)
    return dict(payload)


def assessment_current(workspace: str | Path, memory: Any, report_id: str, *,
                       current_policy: ReleasePolicy | None = None) -> dict[str, Any]:
    """Old assessments stay historical; currentness is recomputed, never rewritten."""
    root = Path(workspace).expanduser().resolve(strict=True)
    payload = _json_file(root, f".paperforge/release/{_identifier(report_id)}/report.json")
    stored = str(payload.get("report_digest") or "")
    stable = {k: payload[k] for k in payload
              if k not in {"report_id", "report_digest", "created_at", "report_file_sha256"}}
    valid = (stored == _digest(stable) and report_id == f"release-{stored[:16]}"
             and payload.get("report_id") == report_id)
    current = False
    registry = verify_registered_bundle(root, memory, {
        f".paperforge/release/{report_id}/report.json": "release",
        f".paperforge/release/{report_id}/manifest.json": "manifest"})
    if valid:
        try:
            policy = ReleasePolicy(**{k: v for k, v in payload["policy"].items() if k != "schema"})
            fresh = evaluate_release(root, memory, ReleaseTarget(**payload["target"]), policy,
                                     **payload["assessment_inputs"])
            current = fresh["report_digest"] == stored
            if current_policy is not None:
                current = current and current_policy.digest() == payload["policy_digest"]
        except (OSError, ValueError, TypeError, KeyError, ReleaseEligibilityError):
            current = False
    return {"report_id": report_id, "report_digest_valid": valid,
            "historical_integrity": "valid" if valid else "invalid",
            **registry,
            "currently_valid": bool(valid and current and registry["registered_complete"]),
            "overall_eligibility": payload.get("overall_eligibility")}


def record_release_acknowledgement(
    workspace: str | Path, memory: Any, report_id: str, *, approver: str, rationale: str,
    current_policy: ReleasePolicy | None = None,
) -> dict[str, Any]:
    """Record an explicit operator action; callers must authenticate the operator.

    This local audit record is not a digital signature or scientific approval.
    No evaluator, retry, or status refresh calls this function.
    """
    root = Path(workspace).expanduser().resolve(strict=True)
    state = assessment_current(root, memory, report_id, current_policy=current_policy)
    if not state["currently_valid"] or state["overall_eligibility"] != "ELIGIBLE":
        raise ReleaseEligibilityError("only a current eligible assessment can be acknowledged")
    if not approver.strip() or not rationale.strip():
        raise ReleaseEligibilityError("explicit approver and rationale required")
    report = _json_file(root, f".paperforge/release/{_identifier(report_id)}/report.json")
    stable = {
        "schema": "paperforge.release-acknowledgement/v1", "report_id": report_id,
        "assessment_digest": report["report_digest"], "target_digest": _digest(report["target"]),
        "policy_digest": report["policy_digest"], "approver": approver, "rationale": rationale,
        "decision": "acknowledged", "recorded_at": utc_now(),
    }
    digest = _digest(stable)
    ack_id = f"ack-{digest[:16]}"
    payload = {**stable, "ack_id": ack_id, "ack_digest": digest}
    atomic_write_text(root, f".paperforge/release_acknowledgements/{ack_id}.json",
                      json.dumps(payload, sort_keys=True, indent=2) + "\n")
    return payload


def release_authorization(workspace: str | Path, memory: Any, report_id: str, *,
                          ack_id: str = "", current_policy: ReleasePolicy | None = None) -> dict[str, Any]:
    """Eligibility != authorization != publication. U4B performs no release."""
    root = Path(workspace).expanduser().resolve(strict=True)
    state = assessment_current(root, memory, report_id, current_policy=current_policy)
    report = _json_file(root, f".paperforge/release/{_identifier(report_id)}/report.json")
    authorization = "BLOCKED"
    if state["currently_valid"] and state["overall_eligibility"] == "ELIGIBLE":
        authorization = "NOT_REQUIRED" if not report["policy"]["require_human_owner_ack"] else "NEEDS_HUMAN"
        if ack_id:
            try:
                ack = _json_file(root, f".paperforge/release_acknowledgements/{_identifier(ack_id)}.json")
                stable = {k: v for k, v in ack.items() if k not in {"ack_id", "ack_digest"}}
                valid = (
                    ack.get("schema") == "paperforge.release-acknowledgement/v1"
                    and ack.get("ack_digest") == _digest(stable)
                    and ack.get("ack_id") == ack_id == f"ack-{_digest(stable)[:16]}"
                    and ack.get("report_id") == report_id
                    and ack.get("assessment_digest") == report["report_digest"]
                    and ack.get("target_digest") == _digest(report["target"])
                    and ack.get("policy_digest") == report["policy_digest"]
                    and ack.get("decision") == "acknowledged"
                    and str(ack.get("approver", "")).strip()
                    and str(ack.get("recorded_at", "")).strip()
                )
                if valid:
                    authorization = "AUTHORIZED"
            except (OSError, ValueError, TypeError, ReleaseEligibilityError):
                pass
    return {**state, "authorization_state": authorization, "release_state": "NOT_RELEASED"}


__all__ = [
    "CATEGORIES",
    "EngineeringCheckResult",
    "POLICY_SCHEMA",
    "POLICY_VERSION",
    "REPORT_SCHEMA",
    "ReleaseEligibilityError",
    "ReleaseGateResult",
    "ReleasePolicy",
    "ReleaseTarget",
    "assessment_current",
    "evaluate_release",
    "freeze_assessment",
    "record_release_acknowledgement",
    "release_authorization",
]
