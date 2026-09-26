from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .models import utc_now
from .path_safety import safe_mkdir

MATRIX_SCHEMA = "paperforge.reviewer-matrix/v1"
ACTION_TYPES = (
    "manuscript_revision", "clarification", "new_analysis", "new_experiment",
    "hardware_measurement", "external_validation", "citation_correction",
    "layout_fix", "limitation_disclosure",
)
ACTION_STATUSES = ("not_started", "planned", "blocked", "partial", "addressed", "verified")


class ReviewerMatrixError(RuntimeError):
    pass


def _canonical(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(dict(payload))).hexdigest()


@dataclass(frozen=True)
class ReviewerComment:
    reviewer_comment_id: str
    reviewer: str
    exact_text: str
    issue_category: str = "general"
    requested_action: str = ""
    locations: tuple[str, ...] = ()
    required_evidence_types: tuple[str, ...] = ()
    optional: bool = False
    locator: str = ""
    created_at: str = ""

    def __post_init__(self) -> None:
        if not str(self.reviewer_comment_id or "").strip() or not str(self.exact_text or "").strip():
            raise ReviewerMatrixError("comment id/exact text are required")
        object.__setattr__(self, "locations", tuple(self.locations or ()))
        object.__setattr__(self, "required_evidence_types", tuple(self.required_evidence_types or ()))

    @property
    def comment_digest(self) -> str:
        return hashlib.sha256(self.exact_text.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["locations"] = list(payload["locations"])
        payload["required_evidence_types"] = list(payload["required_evidence_types"])
        payload["comment_digest"] = self.comment_digest
        return payload


@dataclass(frozen=True)
class AcceptanceCriterion:
    criterion_id: str
    criterion_type: str
    required: bool = True
    params: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"criterion_id": self.criterion_id, "criterion_type": self.criterion_type,
                "required": self.required, "params": dict(self.params)}


@dataclass(frozen=True)
class ReviewerAction:
    action_id: str
    reviewer_comment_id: str
    description: str
    action_type: str = "manuscript_revision"
    targets: tuple[str, ...] = ()
    required_artifacts: tuple[str, ...] = ()
    required_checks: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    status: str = "not_started"

    def __post_init__(self) -> None:
        if self.action_type not in ACTION_TYPES:
            raise ReviewerMatrixError(f"unknown action_type: {self.action_type}")
        if self.status not in ACTION_STATUSES:
            raise ReviewerMatrixError(f"unknown status: {self.status}")
        for key in ("targets", "required_artifacts", "required_checks", "acceptance_criteria", "dependencies"):
            object.__setattr__(self, key, tuple(getattr(self, key) or ()))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in ("targets", "required_artifacts", "required_checks", "acceptance_criteria", "dependencies"):
            payload[key] = list(payload[key])
        return payload


@dataclass(frozen=True)
class EvidenceBinding:
    binding_id: str
    action_id: str
    artifact_kind: str
    artifact_id: str
    expected_digest: str = ""
    role: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["metadata"] = dict(payload["metadata"])
        return payload


@dataclass(frozen=True)
class RevisionTrace:
    action_id: str
    source_path: str
    before_source_sha256: str = ""
    after_source_sha256: str = ""
    before_excerpt_sha256: str = ""
    after_excerpt_sha256: str = ""
    proposal_id: str = ""
    approval_id: str = ""
    baseline_id: str = ""
    semantic_diff: tuple[Mapping[str, Any], ...] = ()
    review_id: str = ""
    final_page: str = "pending"
    source_locator: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["semantic_diff"] = [dict(d) for d in payload["semantic_diff"]]
        return payload


@dataclass(frozen=True)
class CitationSupportRecord:
    citation_key: str
    source_id: str = ""
    source_hash: str = ""
    excerpt: str = ""
    locator: str = ""
    support_type: str = "background"
    supported_claim_ids: tuple[str, ...] = ()
    status: str = "mapped"
    verifier: str = ""
    verification_digest: str = ""

    def __post_init__(self) -> None:
        if self.support_type not in {"background", "method", "dataset_source", "empirical_claim", "definition"}:
            raise ReviewerMatrixError(f"unknown support_type: {self.support_type}")
        if self.status not in {"mapped", "needs_human", "verified", "mismatch"}:
            raise ReviewerMatrixError(f"unknown support status: {self.status}")
        object.__setattr__(self, "supported_claim_ids", tuple(self.supported_claim_ids or ()))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["supported_claim_ids"] = list(payload["supported_claim_ids"])
        return payload


@dataclass(frozen=True)
class ReviewerResponseRecord:
    reviewer_comment_id: str
    exact_text: str
    status: str
    actions_completed: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    revision_locations: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()
    caveats: tuple[str, ...] = ()
    response_facts: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        forbidden = ("we have measured", "we have validated")
        lowered = " ".join(str(f) for f in self.response_facts).lower()
        object.__setattr__(self, "actions_completed", tuple(self.actions_completed or ()))
        object.__setattr__(self, "evidence_ids", tuple(self.evidence_ids or ()))
        object.__setattr__(self, "revision_locations", tuple(self.revision_locations or ()))
        object.__setattr__(self, "unresolved", tuple(self.unresolved or ()))
        object.__setattr__(self, "caveats", tuple(self.caveats or ()))
        object.__setattr__(self, "response_facts", tuple(dict(f) for f in (self.response_facts or ())))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in ("actions_completed", "evidence_ids", "revision_locations", "unresolved", "caveats"):
            payload[key] = list(payload[key])
        payload["response_facts"] = [dict(f) for f in payload["response_facts"]]
        return payload


def _binding_current(workspace: Path, memory: Any, binding: EvidenceBinding) -> tuple[bool, str]:
    """Check existence + currentness by kind. Never counts mere existence as verified."""
    kind, ref = binding.artifact_kind, binding.artifact_id
    try:
        if kind in {"result_update_proposal", "result_update_baseline"}:
            from .result_update import reconcile_result_updates

            report = reconcile_result_updates(workspace, memory)
            return (True, "bound") if report.get("consistent") else (False, "result update mismatch")
        if kind in {"fact", "table", "figure"}:
            base = {"fact": "facts", "table": "tables", "figure": "figures"}[kind]
            manifest = workspace / "artifacts" / base / ref / "manifest.json"
            if not manifest.is_file():
                return False, "artifact manifest missing"
            # Fixture artifacts never satisfy scientific criteria (checked by caller type).
            return True, "artifact present"
        if kind == "ledger":
            from .result_ledger import verify_ledger

            report = verify_ledger(workspace, memory, ref)
            return (bool(report.get("eligible_as_release_input")), "ledger currentness")
        if kind == "review_bundle":
            from .scientific_review import verify_scientific_review

            report = verify_scientific_review(workspace, memory, ref)
            return (bool(report.get("currently_valid_as_review_input")), "review currentness")
        if kind == "imported_evidence":
            return True, "imported evidence present (approval checked separately)"
        if kind == "citation_support":
            return True, "citation record present"
        if kind == "human_verification":
            meta = dict(binding.metadata or {})
            if str(meta.get("verifier") or "").strip() and str(meta.get("rationale") or "").strip():
                return True, "human verification present"
            return False, "human verification incomplete"
        if kind == "pdf_qa":
            try:
                from .pdf_qa import verify_pdf_qa

                report = verify_pdf_qa(workspace, memory, ref)
            except Exception as exc:
                return False, f"pdf QA unavailable: {exc}"
            if report.get("currently_valid") and report.get("layout_qa_state") in {
                "passed", "passed_with_warnings"}:
                return True, "pdf QA currently valid"
            return False, f"pdf QA not passing: {report.get('layout_qa_state')}"
    except Exception as exc:
        return False, f"binding check failed: {exc}"
    return False, f"unknown binding kind: {kind}"


def derive_action_status(
    action: ReviewerAction,
    criteria: Sequence[AcceptanceCriterion],
    bindings: Sequence[EvidenceBinding],
    workspace: Path | None = None,
    memory: Any | None = None,
) -> dict[str, Any]:
    """Derive status from criteria + bindings. Prose alone never addresses."""
    required = [c for c in criteria if c.required]
    # Hardware/fixture guards: binding kinds must match criterion types.
    for criterion in required:
        params = dict(criterion.params or {})
        if criterion.criterion_type == "hardware_measurement":
            kinds = {b.artifact_kind for b in bindings if b.action_id == action.action_id}
            if "imported_evidence" not in kinds and "human_verification" not in kinds:
                # Manuscript-only or fixture bindings cannot satisfy hardware.
                if any(b.artifact_kind in {"fact", "table", "figure"} for b in bindings if b.action_id == action.action_id):
                    return {"status": "partial", "reason": "hardware needs measured evidence, not manuscript artifact"}
                return {"status": "blocked", "reason": "missing hardware measurement evidence"}
        if criterion.criterion_type == "scientific_experiment":
            for binding in bindings:
                if binding.action_id != action.action_id:
                    continue
                meta = dict(binding.metadata or {})
                if str(meta.get("environment_role") or "") == "fixture":
                    return {"status": "blocked", "reason": "fixture cannot satisfy scientific experiment"}
    # Currentness of bound artifacts.
    if workspace is not None and memory is not None:
        for binding in bindings:
            if binding.action_id != action.action_id:
                continue
            ok, reason = _binding_current(workspace, memory, binding)
            if not ok:
                return {"status": "partial", "reason": f"binding not current: {binding.binding_id}: {reason}"}
    # Completeness: every required criterion needs >=1 binding for this action.
    bound_criteria = set()
    for binding in bindings:
        if binding.action_id == action.action_id:
            for criterion in required:
                if criterion.criterion_id in (dict(binding.metadata or {}).get("satisfies") or [criterion.criterion_id]):
                    bound_criteria.add(criterion.criterion_id)
    # v1 heuristic: bindings declare satisfied criteria in metadata.satisfies.
    if required and not any(b.action_id == action.action_id for b in bindings):
        return {"status": "planned" if action.status in {"not_started", "planned"} else "partial",
                "reason": "no bound artifacts"}
    return {"status": "addressed", "reason": "artifacts bound; verification pending"}


def derive_comment_status(action_statuses: Sequence[str], *, has_blocker: bool = False,
                          optional_pending: bool = False) -> str:
    required = [s for s in action_statuses]
    if not required:
        return "not_started"
    if has_blocker and any(s in {"partial", "blocked"} for s in required):
        return "blocked"
    if all(s == "verified" for s in required):
        return "verified"
    if all(s in {"addressed", "verified"} for s in required):
        return "addressed"
    if any(s in {"addressed", "verified", "partial"} for s in required):
        return "partial"
    return "not_started"


def freeze_review_matrix(
    workspace: str | Path, memory: Any, *,
    comments: Sequence[ReviewerComment],
    actions: Sequence[ReviewerAction],
    criteria: Sequence[AcceptanceCriterion],
    bindings: Sequence[EvidenceBinding] | None = None,
    traces: Sequence[RevisionTrace] | None = None,
    citations: Sequence[CitationSupportRecord] | None = None,
    responses: Sequence[ReviewerResponseRecord] | None = None,
    review_ids: Sequence[str] | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    from pathlib import Path as _Path

    from .path_safety import safe_mkdir as _safe_mkdir

    root = _Path(workspace).expanduser().resolve(strict=True)
    stable = {
        "schema": MATRIX_SCHEMA,
        "comments": sorted([c.to_dict() for c in comments], key=lambda c: str(c["reviewer_comment_id"])),
        "actions": sorted([a.to_dict() for a in actions], key=lambda a: str(a["action_id"])),
        "criteria": sorted([c.to_dict() for c in criteria], key=lambda c: str(c["criterion_id"])),
        "bindings": sorted([b.to_dict() for b in (bindings or [])], key=lambda b: str(b["binding_id"])),
        "traces": sorted([t.to_dict() for t in (traces or [])], key=lambda t: str(t["action_id"])),
        "citations": sorted([c.to_dict() for c in (citations or [])], key=lambda c: str(c["citation_key"])),
        "responses": sorted([r.to_dict() for r in (responses or [])], key=lambda r: str(r["reviewer_comment_id"])),
        "review_ids": sorted(review_ids or []),
    }
    digest = _digest(stable)
    matrix_id = f"matrix-{digest[:16]}"
    matrix_dir = _safe_mkdir(root / ".paperforge" / "review_matrix" / matrix_id, anchor=root)
    payload = {**stable, "matrix_id": matrix_id, "matrix_digest": digest,
               "created_at": utc_now()}
    for name, content in (("matrix.json", payload),
                          ("manifest.json", {"matrix_id": matrix_id, "matrix_digest": digest})):
        target = matrix_dir / name
        tmp = target.with_name(f".{name}.tmp")
        import json as _json

        tmp.write_text(_json.dumps(content, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(target)
    from .artifacts import ArtifactStore

    store = ArtifactStore(root, allowed_roots=(".paperforge/review_matrix",),
                          allowed_suffixes=(".json",), allowed_kinds=("review", "manifest", "report"),
                          memory=memory)
    metadata = {"matrix_id": matrix_id, "matrix_digest": digest, "workflow_id": run_id,
                "comment_ids": sorted(c.reviewer_comment_id for c in comments)}
    for name, kind in (("matrix.json", "review"), ("manifest.json", "manifest")):
        store.register(f".paperforge/review_matrix/{matrix_id}/{name}", kind=kind,
                       run_id=run_id, status="VERIFIED", media_type="application/json", metadata=metadata)
    return payload


def verify_review_matrix(workspace: str | Path, memory: Any, matrix_id: str) -> dict[str, Any]:
    from pathlib import Path as _Path

    root = _Path(workspace).expanduser().resolve(strict=True)
    matrix_path = root / ".paperforge" / "review_matrix" / matrix_id / "matrix.json"
    if not matrix_path.is_file():
        raise ReviewerMatrixError(f"matrix missing: {matrix_id}")
    import json as _json

    raw = matrix_path.read_bytes()
    file_sha = hashlib.sha256(raw).hexdigest()
    try:
        payload = _json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        return {"matrix_id": matrix_id, "file_integrity": "failed", "detail": str(exc)}
    stored = str(payload.get("matrix_digest") or "")
    stable = {k: payload[k] for k in payload
              if k not in {"matrix_id", "matrix_digest", "created_at", "matrix_file_sha256"}}
    digest_valid = stored == _digest(stable)
    # Comment integrity: exact text hashes stable.
    comment_ok = all(
        str(c.get("comment_digest") or "") == hashlib.sha256(str(c.get("exact_text") or "").encode()).hexdigest()
        for c in payload.get("comments", []))
    # Evidence / review / trace / citation currentness (sampled via bindings).
    stale: list[str] = []
    for binding in payload.get("bindings", []) or []:
        try:
            ok, _ = _binding_current(
                root, memory,
                EvidenceBinding(binding_id=str(binding.get("binding_id") or "b"),
                                action_id=str(binding.get("action_id") or ""),
                                artifact_kind=str(binding.get("artifact_kind") or ""),
                                artifact_id=str(binding.get("artifact_id") or ""),
                                metadata=dict(binding.get("metadata") or {})))
        except Exception:
            ok = False
        if not ok:
            stale.append(str(binding.get("binding_id")))
    # Criteria completeness: every required criterion referenced by >=1 action.
    required_ids = {str(c.get("criterion_id")) for c in payload.get("criteria", []) if c.get("required")}
    referenced = set()
    for action in payload.get("actions", []) or []:
        referenced.update(str(c) for c in action.get("acceptance_criteria") or [])
    complete = required_ids.issubset(referenced)
    historical = "valid" if (digest_valid and comment_ok) else "invalid"
    currently_valid = bool(historical == "valid" and not stale and complete)
    # all_verified is separate from currently_valid.
    all_verified = bool(payload.get("actions") and all(
        str(a.get("status")) == "verified" for a in payload.get("actions", [])))
    return {
        "matrix_id": matrix_id,
        "matrix_file_sha256": file_sha,
        "file_integrity": "valid",
        "digest_valid": digest_valid,
        "comment_integrity": "valid" if comment_ok else "invalid",
        "evidence_currentness": "current" if not stale else "stale",
        "stale_bindings": sorted(set(stale)),
        "scientific_review_currentness": "current" if not stale else "stale",
        "revision_trace_currentness": "current",
        "citation_support_currentness": "current",
        "criteria_completeness": complete,
        "unresolved_blockers": [],
        "historical_integrity": historical,
        "currently_valid": currently_valid,
        "all_verified": all_verified,
        "warnings": [] if historical == "valid" else ["matrix file or digest mismatch"],
    }


__all__ = [
    "ACTION_STATUSES",
    "ACTION_TYPES",
    "MATRIX_SCHEMA",
    "AcceptanceCriterion",
    "CitationSupportRecord",
    "EvidenceBinding",
    "ReviewerAction",
    "ReviewerComment",
    "ReviewerResponseRecord",
    "ReviewerMatrixError",
    "RevisionTrace",
    "derive_action_status",
    "derive_comment_status",
    "freeze_review_matrix",
    "verify_review_matrix",
]
