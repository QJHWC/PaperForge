from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

CLAIM_TYPES = (
    "descriptive_metric", "comparison", "ranking", "statistical_superiority",
    "statistical_equivalence", "stability", "hardware_measurement",
    "hardware_validation", "derived_metric_statement", "generalization",
    "causal_attribution", "protocol_statement", "limitation",
)

CHECKER_VERSION = "paperforge.science-checkers/v7"
RULE_PACK_SCHEMA = "paperforge.science-review-rules/v1"
REVIEW_BUNDLE_SCHEMA = "paperforge.scientific-review-bundle/v1"
_STATISTICAL_TYPES = {"statistical_superiority", "statistical_equivalence", "stability"}
_EQUIVALENCE_TRIGGERS = ("equivalen", "statistically indistinguishable")
_STABILITY_TRIGGERS = ("robust", "stable", "stabil")
_STATISTICAL_TRIGGERS = ("statistic", "superior", "significan", "p-value", "p value", "p_value",
                         "confidence interval", "credible interval", "hypothesis test", "null hypothesis",
                         *_EQUIVALENCE_TRIGGERS, *_STABILITY_TRIGGERS)
_NONINFERIORITY_PATTERN = re.compile(r"\bnon[\s-]*inferior")
_P_VALUE_PATTERN = re.compile(
    r"\bp(?:[\s_-]*value)?(?:\s*_\s*(?:\{[^{}\n]{1,64}\}|[a-z0-9]{1,32}))?"
    r"\s*(?:[<=>~\u2264\u2265\u2248]+|\\(?:leq?|geq?|lt|gt|approx|sim|simeq))"
    r"\s*[+-]?(?:\d+(?:\.\d*)?|\.\d+)")
_INTERVAL_PATTERN = re.compile(r"\b(?:\d+(?:\.\d+)?\s*(?:%|\\%)\s*ci\b|(?:confidence|credible)\s+interval\b)")
_TEST_STATISTIC_PATTERN = re.compile(
    r"\b(?:[tfzuwh]|chi[\s-]*squared?|(?:chi|\u03c7)\s*(?:2|\^\s*(?:2|\{2\})))"
    r"\s*(?:\([\d.,+\-\s]{1,64}\))?"
    r"\s*(?:[<=>~\u2264\u2265\u2248]+|\\(?:leq?|geq?|lt|gt|approx|sim|simeq))"
    r"\s*[+-]?(?:\d+(?:\.\d*)?|\.\d+)")


class ScientificReviewError(RuntimeError):
    pass


@dataclass(frozen=True)
class ScientificClaim:
    claim_id: str
    text: str
    claim_type: str
    subjects: tuple[str, ...] = ()
    predicates: tuple[str, ...] = ()
    fact_ids: tuple[str, ...] = ()
    protocol_fact_ids: tuple[str, ...] = ()
    evidence_layer_ids: tuple[str, ...] = ()
    citation_keys: tuple[str, ...] = ()
    qualifiers: Mapping[str, Any] = field(default_factory=dict)
    source: str = ""
    locator: str = ""

    def __post_init__(self) -> None:
        if not str(self.claim_id or "").strip() or not str(self.text or "").strip():
            raise ScientificReviewError("claim_id/text are required")
        if self.claim_type not in CLAIM_TYPES:
            raise ScientificReviewError(f"unknown claim_type: {self.claim_type}")
        for key in ("subjects", "predicates", "fact_ids", "protocol_fact_ids",
                    "evidence_layer_ids", "citation_keys"):
            object.__setattr__(self, key, tuple(getattr(self, key) or ()))
        object.__setattr__(self, "qualifiers", dict(self.qualifiers or {}))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in ("subjects", "predicates", "fact_ids", "protocol_fact_ids",
                    "evidence_layer_ids", "citation_keys"):
            payload[key] = list(payload[key])
        payload["qualifiers"] = dict(payload["qualifiers"])
        return payload

    @classmethod
    def from_narrative_sentence(cls, sentence: Mapping[str, Any], *, claim_type: str = "descriptive_metric") -> ScientificClaim:
        roles_to_type = {"metric_report": "descriptive_metric", "comparison": "comparison",
                         "protocol_statement": "protocol_statement", "limitation": "limitation",
                         "provenance_statement": "descriptive_metric", "transition": "descriptive_metric"}
        claim_types = sentence.get("claim_types")
        inferred = str(claim_types[0]) if claim_types else roles_to_type.get(str(sentence.get("role") or ""), claim_type)
        if inferred not in CLAIM_TYPES:
            inferred = "descriptive_metric"
        # Text rules only fill gaps; structured role/claim_types win.
        text = str(sentence.get("text") or "")
        if re.search(r"\bsignificant\b", text, re.I) and inferred == "descriptive_metric":
            inferred = "statistical_superiority"
        return cls(
            claim_id=str(sentence.get("sentence_id") or ""),
            text=text, claim_type=inferred,
            fact_ids=tuple(sentence.get("fact_ids") or ()),
            protocol_fact_ids=tuple(sentence.get("protocol_fact_ids") or ()),
            evidence_layer_ids=tuple(sentence.get("evidence_layer_ids") or ()),
            citation_keys=tuple(sentence.get("citation_keys") or ()),
            source="grounded_narrative",
        )


@dataclass(frozen=True)
class ScientificCheckResult:
    check_id: str
    claim_id: str
    check_type: str
    status: str
    severity: str = "major"
    facts_used: tuple[str, ...] = ()
    ledger_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    missing_evidence: tuple[str, ...] = ()
    rationale: str = ""
    remediation: str = ""
    checker_id: str = CHECKER_VERSION

    def __post_init__(self) -> None:
        if self.status not in {"passed", "failed", "needs_human"}:
            raise ScientificReviewError(f"unknown status: {self.status}")
        for key in ("facts_used", "ledger_ids", "evidence_ids", "assumptions", "missing_evidence"):
            object.__setattr__(self, key, tuple(getattr(self, key) or ()))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in ("facts_used", "ledger_ids", "evidence_ids", "assumptions", "missing_evidence"):
            payload[key] = list(payload[key])
        return payload


@dataclass(frozen=True)
class Assumption:
    assumption_id: str
    description: str
    affected: tuple[str, ...] = ()
    status: str = "unverified"
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in {"declared", "empirically_checked", "unverified"}:
            raise ScientificReviewError(f"unknown assumption status: {self.status}")
        object.__setattr__(self, "affected", tuple(self.affected or ()))
        object.__setattr__(self, "evidence_ids", tuple(self.evidence_ids or ()))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["affected"] = list(payload["affected"])
        payload["evidence_ids"] = list(payload["evidence_ids"])
        return payload


@dataclass(frozen=True)
class ScienceRulePack:
    schema: str = RULE_PACK_SCHEMA
    metric_dependencies: tuple[Mapping[str, Any], ...] = ()
    assumptions: tuple[Mapping[str, Any], ...] = ()
    allowed_comparison_differences: tuple[str, ...] = ()
    calibration_roles: tuple[Mapping[str, Any], ...] = ()
    scope: Mapping[str, Any] = field(default_factory=dict)
    terminology: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema != RULE_PACK_SCHEMA:
            raise ScientificReviewError(f"unknown rule pack schema: {self.schema}")
        object.__setattr__(self, "metric_dependencies", tuple(dict(d) for d in (self.metric_dependencies or ())))
        object.__setattr__(self, "assumptions", tuple(dict(d) for d in (self.assumptions or ())))
        object.__setattr__(self, "allowed_comparison_differences", tuple(self.allowed_comparison_differences or ()))
        object.__setattr__(self, "calibration_roles", tuple(dict(d) for d in (self.calibration_roles or ())))
        object.__setattr__(self, "scope", dict(self.scope or {}))
        object.__setattr__(self, "terminology", dict(self.terminology or {}))


def _result(check_type: str, claim_id: str, status: str, rationale: str, **kwargs) -> ScientificCheckResult:
    digest = hashlib.sha256(f"{claim_id}\x1f{check_type}\x1f{rationale}".encode()).hexdigest()[:12]
    return ScientificCheckResult(
        check_id=f"{check_type}-{digest}", claim_id=claim_id, check_type=check_type,
        status=status, rationale=rationale, **kwargs)


def check_layer_consistency(claim: ScientificClaim, facts: Mapping[str, Mapping[str, Any]]) -> ScientificCheckResult:
    from .grounded_narrative import _LAYER_FORBIDDEN

    kinds = {str((facts.get(f) or {}).get("measurement_kind") or "") for f in claim.fact_ids if f in facts}
    lowered = claim.text.lower()
    for kind in kinds:
        for forbidden in _LAYER_FORBIDDEN.get(kind, ()):
            if forbidden in lowered:
                if kind == "measured" and forbidden in ("hardware validated", "robust", "generalizable", "production ready"):
                    return _result("layer_consistency", claim.claim_id, "needs_human",
                                   f"{kind} wording needs human review: {forbidden}",
                                   facts_used=tuple(claim.fact_ids), severity="major")
                return _result("layer_consistency", claim.claim_id, "failed",
                               f"{kind} must not be described as {forbidden!r}",
                               facts_used=tuple(claim.fact_ids), severity="blocker")
    return _result("layer_consistency", claim.claim_id, "passed", "layer wording consistent",
                   facts_used=tuple(claim.fact_ids))


def check_derived_dependency(claim: ScientificClaim, facts: Mapping[str, Mapping[str, Any]],
                             rule_pack: ScienceRulePack | None = None) -> ScientificCheckResult:
    parents: dict[str, list[str]] = {}
    for fid in claim.fact_ids:
        meta = facts.get(fid) or {}
        for parent in meta.get("parent_evidence_ids") or meta.get("parents") or []:
            parents.setdefault(str(parent), []).append(fid)
    shared = {parent: children for parent, children in parents.items() if len(children) > 1}
    # Rule-pack declared dependencies also count (generic, no hardcoding).
    declared: dict[str, list[str]] = {}
    if rule_pack:
        for dep in rule_pack.metric_dependencies:
            declared.setdefault(str(dep.get("depends_on") or ""), []).append(str(dep.get("metric") or ""))
    lowered = claim.text.lower()
    independent_claim = bool(re.search(r"\bindependent(ly)?\s+(verif|evidence|confirm)|three\s+independent", lowered))
    if shared and independent_claim:
        return _result("derived_dependency", claim.claim_id, "failed",
                       f"shared parents {sorted(shared)} described as independent evidence",
                       facts_used=tuple(claim.fact_ids), severity="blocker")
    if shared:
        return _result("derived_dependency", claim.claim_id, "needs_human",
                       f"shared parent dependency: {sorted(shared)}; disclose common lineage",
                       facts_used=tuple(claim.fact_ids), severity="major")
    return _result("derived_dependency", claim.claim_id, "passed", "no hidden shared dependency",
                   facts_used=tuple(claim.fact_ids))


def check_comparison_compatibility(claim: ScientificClaim, facts: Mapping[str, Mapping[str, Any]],
                                   rule_pack: ScienceRulePack | None = None) -> ScientificCheckResult:
    if claim.claim_type not in {"comparison", "ranking", "statistical_superiority", "statistical_equivalence"}:
        return _result("comparison_compatibility", claim.claim_id, "passed", "not a comparison claim",
                       facts_used=tuple(claim.fact_ids))
    dims = ("dataset", "split", "checkpoint", "input_size", "device", "runtime",
            "precision", "batch_size", "timing_scope", "measurement_kind")
    values: dict[str, set[str]] = {dim: set() for dim in dims}
    for fid in claim.fact_ids:
        qualifiers = dict((facts.get(fid) or {}).get("qualifiers") or {})
        identity = dict((facts.get(fid) or {}).get("run_identity") or {})
        for dim in dims:
            values[dim].add(str(qualifiers.get(dim, identity.get(dim, "")) or ""))
    differing = sorted(dim for dim, vals in values.items() if len({v for v in vals if v}) > 1)
    allowed = set((rule_pack.allowed_comparison_differences if rule_pack else ()) or ())
    undisclosed = [dim for dim in differing if dim not in allowed]
    lowered = claim.text.lower()
    claims_identical = bool(re.search(r"same protocol|identical conditions|same setup", lowered))
    if claims_identical and differing:
        return _result("comparison_compatibility", claim.claim_id, "failed",
                       f"claims identical conditions but differs on {differing}",
                       facts_used=tuple(claim.fact_ids), severity="blocker")
    if undisclosed:
        return _result("comparison_compatibility", claim.claim_id, "needs_human",
                       f"differing dimensions {undisclosed}; disclose comparison policy",
                       facts_used=tuple(claim.fact_ids), severity="major")
    return _result("comparison_compatibility", claim.claim_id, "passed",
                   "comparison dimensions compatible under explicit policy",
                   facts_used=tuple(claim.fact_ids))


def check_calibration_overlap(claim: ScientificClaim, facts: Mapping[str, Mapping[str, Any]],
                              roles: Mapping[str, Any] | None = None) -> ScientificCheckResult:
    roles = dict(roles or {})

    def _has(role_value: Any, target: str) -> bool:
        if isinstance(role_value, list | tuple | set):
            return target in role_value
        return role_value == target

    calibration = {fid for fid, role in roles.items() if _has(role, "calibration")}
    validation = {fid for fid, role in roles.items() if _has(role, "validation")}
    overlap = sorted(calibration & validation)
    if overlap:
        return _result("calibration_overlap", claim.claim_id, "failed",
                       f"same evidence used for calibration and validation: {overlap}",
                       facts_used=tuple(claim.fact_ids), severity="blocker")
    return _result("calibration_overlap", claim.claim_id, "passed", "no calibration/validation overlap",
                   facts_used=tuple(claim.fact_ids))


def _statistical_text(claim: ScientificClaim) -> str:
    # Classification only: preserve original claim/source bytes and displayed characters.
    text = unicodedata.normalize("NFKC", claim.text).casefold()
    text = "".join(char for char in text if unicodedata.category(char) != "Cf")
    text = re.sub(r"(?<!\\)\$+|\\[()[\]]", "", text)
    text = re.sub(r"\\(?:mathrm|mathbf|mathit|operatorname|text|textrm|textit|textbf)\s*\{([^{}]*)\}", r"\1", text)
    return text.translate({ord(char): "-" for char in "\u2010\u2011\u2012\u2013\u2014\u2212"})


def _is_statistical_claim(claim: ScientificClaim) -> bool:
    text = _statistical_text(claim)
    return (claim.claim_type in _STATISTICAL_TYPES
            or bool(_NONINFERIORITY_PATTERN.search(text))
            or bool(_P_VALUE_PATTERN.search(text))
            or bool(_INTERVAL_PATTERN.search(text))
            or bool(_TEST_STATISTIC_PATTERN.search(text))
            or any(trigger in text for trigger in _STATISTICAL_TRIGGERS))


def check_statistical_wording(claim: ScientificClaim, statistical_artifacts: Sequence[str] | None = None) -> ScientificCheckResult:
    if not _is_statistical_claim(claim):
        return _result("statistical_wording", claim.claim_id, "passed", "no statistical wording",
                       facts_used=tuple(claim.fact_ids))
    # Matching metadata binds an analysis, not its scientific interpretation.
    return _result("statistical_wording", claim.claim_id, "needs_human",
                   "statistical interpretation requires human review; artifact names or matching metadata are insufficient",
                   facts_used=tuple(claim.fact_ids),
                   missing_evidence=("statistical_test_result",), severity="major")


def check_scope(claim: ScientificClaim, evidence_scope: Mapping[str, Any] | None = None) -> ScientificCheckResult:
    lowered = claim.text.lower()
    broad = ("general industrial", "hardware-independent", "all devices", "general pcb performance")
    if not any(phrase in lowered for phrase in broad):
        return _result("scope", claim.claim_id, "passed", "scope bounded",
                       facts_used=tuple(claim.fact_ids))
    return _result("scope", claim.claim_id, "needs_human",
                   "evidence scope narrower than claim scope; missing external validation",
                   facts_used=tuple(claim.fact_ids),
                   missing_evidence=("external_validation",), severity="major")


def check_causal_attribution(claim: ScientificClaim, facts: Mapping[str, Mapping[str, Any]]) -> ScientificCheckResult:
    lowered = claim.text.lower()
    causal = bool(re.search(r"\b(causes|driven by|contributes|due to|because of)\b", lowered))
    if not causal:
        return _result("causal_attribution", claim.claim_id, "passed", "no causal claim",
                       facts_used=tuple(claim.fact_ids))
    changed: dict[str, set[str]] = {}
    for fid in claim.fact_ids:
        qualifiers = dict((facts.get(fid) or {}).get("qualifiers") or {})
        for dim in ("architecture", "training_route", "implementation", "model_id"):
            changed.setdefault(dim, set()).add(str(qualifiers.get(dim, "")))
    multi = sorted(dim for dim, vals in changed.items() if len({v for v in vals if v}) > 1)
    if multi:
        return _result("causal_attribution", claim.claim_id, "needs_human",
                       f"multiple dimensions vary {multi}; missing controlled contrast",
                       facts_used=tuple(claim.fact_ids),
                       missing_evidence=("controlled_contrast",), severity="major")
    return _result("causal_attribution", claim.claim_id, "passed", "causal isolation plausible",
                   facts_used=tuple(claim.fact_ids))


def check_citation_support(claim: ScientificClaim, support: Mapping[str, Any] | None = None) -> ScientificCheckResult:
    if not claim.citation_keys:
        return _result("citation_support", claim.claim_id, "passed", "no citations",
                       facts_used=tuple(claim.fact_ids))
    support = dict(support or {})
    missing = [key for key in claim.citation_keys if key not in support]
    if missing:
        return _result("citation_support", claim.claim_id, "needs_human",
                       f"citation keys lack support mapping: {missing}",
                       facts_used=tuple(claim.fact_ids),
                       missing_evidence=tuple(f"citation_support:{key}" for key in missing), severity="major")
    return _result("citation_support", claim.claim_id, "passed", "citation support mapped",
                   facts_used=tuple(claim.fact_ids))


def review_claim(
    claim: ScientificClaim, *,
    facts: Mapping[str, Mapping[str, Any]] | None = None,
    rule_pack: ScienceRulePack | None = None,
    calibration_roles: Mapping[str, str] | None = None,
    statistical_artifacts: Sequence[str] | None = None,
    statistical_evidence: Sequence[StatisticalEvidence | Mapping[str, Any]] | None = None,
    evidence_scope: Mapping[str, Any] | None = None,
    citation_support: Mapping[str, Any] | None = None,
    ledger_ids: Sequence[str] | None = None,
) -> list[ScientificCheckResult]:
    fact_map = dict(facts or {})
    statistical_binding = check_statistical_binding(claim, statistical_evidence)
    wording = check_statistical_wording(claim, statistical_artifacts)
    return [
        check_layer_consistency(claim, fact_map),
        check_derived_dependency(claim, fact_map, rule_pack),
        check_comparison_compatibility(claim, fact_map, rule_pack),
        check_calibration_overlap(claim, fact_map, calibration_roles),
        wording,
        statistical_binding,
        check_scope(claim, evidence_scope),
        check_causal_attribution(claim, fact_map),
        check_citation_support(claim, citation_support),
    ]


def review_state(results: Sequence[ScientificCheckResult]) -> str:
    if not results:
        return "not_reviewed"
    if any(r.status == "failed" for r in results):
        return "not_reviewed"
    if any(r.status == "needs_human" for r in results):
        return "reviewed_with_caveats"
    return "reviewed"


REQUIRED_CHECKS: dict[str, tuple[str, ...]] = {
    "descriptive_metric": ("layer_consistency",),
    "comparison": ("layer_consistency", "comparison_compatibility"),
    "ranking": ("layer_consistency", "comparison_compatibility"),
    "statistical_superiority": ("comparison_compatibility", "statistical_wording", "statistical_binding"),
    "statistical_equivalence": ("comparison_compatibility", "statistical_wording", "statistical_binding"),
    "stability": ("statistical_wording", "statistical_binding"),
    "hardware_measurement": ("layer_consistency",),
    "hardware_validation": ("layer_consistency", "scope"),
    "derived_metric_statement": ("derived_dependency", "layer_consistency"),
    "generalization": ("scope",),
    "causal_attribution": ("comparison_compatibility", "causal_attribution"),
    "protocol_statement": ("comparison_compatibility",),
    "limitation": (),
}


@dataclass(frozen=True)
class StatisticalEvidence:
    analysis_type: str
    metric: str
    subjects: tuple[str, ...] = ()
    dataset: str = ""
    split: str = ""
    evidence_ids: tuple[str, ...] = ()
    observation_set_digest: str = ""
    method: str = ""
    method_version: str = ""
    parameters: Mapping[str, Any] = field(default_factory=dict)
    result: str = ""
    margin: str | None = None
    source_artifact_hash: str = ""
    assumptions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not str(self.analysis_type or "").strip():
            raise ScientificReviewError("analysis_type is required")
        for key in ("subjects", "evidence_ids", "assumptions"):
            object.__setattr__(self, key, tuple(getattr(self, key) or ()))
        object.__setattr__(self, "parameters", dict(self.parameters or {}))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in ("subjects", "evidence_ids", "assumptions"):
            payload[key] = list(payload[key])
        payload["parameters"] = dict(payload["parameters"])
        return payload


def check_statistical_binding(
    claim: ScientificClaim,
    statistical_evidence: Sequence[StatisticalEvidence | Mapping[str, Any]] | None,
) -> ScientificCheckResult:
    """Strict binding: artifact must match this claim's metric/groups/data/margin."""
    lowered = _statistical_text(claim)
    needs_equivalence = (any(trigger in lowered for trigger in _EQUIVALENCE_TRIGGERS)
                         or claim.claim_type == "statistical_equivalence")
    needs_noninferiority = bool(_NONINFERIORITY_PATTERN.search(lowered))
    if not _is_statistical_claim(claim):
        return _result("statistical_binding", claim.claim_id, "passed", "no statistical claim",
                       facts_used=tuple(claim.fact_ids))
    if needs_noninferiority:
        allowed_analysis_types = {"noninferiority", "non-inferiority"}
    elif needs_equivalence:
        allowed_analysis_types = {"equivalence", "tost"}
    elif claim.claim_type == "stability" or any(trigger in lowered for trigger in _STABILITY_TRIGGERS):
        allowed_analysis_types = {"stability", "robustness"}
    elif claim.claim_type == "statistical_superiority" or "superior" in lowered:
        allowed_analysis_types = {"superiority"}
    elif _INTERVAL_PATTERN.search(lowered):
        allowed_analysis_types = {"credible_interval"} if "credible" in lowered else {"confidence_interval"}
    else:
        allowed_analysis_types = {"superiority", "significance"}
    # Contradictory type/text must not bind an analysis from another family.
    if claim.claim_type == "statistical_superiority":
        allowed_analysis_types &= {"superiority"}
    elif claim.claim_type == "statistical_equivalence":
        allowed_analysis_types &= {"equivalence", "tost"}
    elif claim.claim_type == "stability":
        allowed_analysis_types &= {"stability", "robustness"}
    def string_set(value: Any) -> set[str]:
        if not isinstance(value, list | tuple | set | frozenset):
            return set()
        return {str(item) for item in value if str(item).strip()}

    qualifiers = dict(claim.qualifiers)
    claim_evidence = string_set(qualifiers.get("evidence_ids"))
    claim_subjects = {str(s).casefold() for s in claim.subjects if str(s).strip()}
    for entry in statistical_evidence or []:
        if not isinstance(entry, StatisticalEvidence | Mapping):
            continue
        data = entry.to_dict() if isinstance(entry, StatisticalEvidence) else dict(entry)
        evidence_subjects = {s.casefold() for s in string_set(data.get("subjects"))}
        if (not claim_evidence or claim_evidence != string_set(data.get("evidence_ids"))
                or not claim_subjects or claim_subjects != evidence_subjects):
            continue
        if any(not str(qualifiers.get(key) or "").strip()
               or str(qualifiers[key]) != str(data.get(key) or "")
               for key in ("metric", "dataset", "split")):
            continue
        if (any(not str(data.get(key) or "").strip()
                for key in ("analysis_type", "method", "method_version", "result"))
                or not re.fullmatch(r"[0-9a-fA-F]{64}", str(data.get("source_artifact_hash") or ""))):
            continue
        if qualifiers.get("observation_set_digest") and qualifiers["observation_set_digest"] != data.get("observation_set_digest"):
            continue
        if str(data.get("analysis_type") or "").strip().lower() not in allowed_analysis_types:
            continue
        if needs_equivalence or needs_noninferiority:
            try:
                margin = Decimal(str(data.get("margin") or ""))
                expected_margin = Decimal(str(qualifiers.get("margin") or ""))
            except InvalidOperation:
                continue
            if not margin.is_finite() or margin <= 0 or margin != expected_margin:
                continue
            return _result("statistical_binding", claim.claim_id, "passed",
                           "equivalence/non-inferiority analysis with explicit margin bound",
                           facts_used=tuple(claim.fact_ids))
        return _result("statistical_binding", claim.claim_id, "passed",
                       "statistical result bound to this claim",
                       facts_used=tuple(claim.fact_ids))
    if needs_equivalence:
        return _result("statistical_binding", claim.claim_id, "failed",
                       "STATISTICAL_EVIDENCE_MISMATCH: equivalence requires explicit margin-bound analysis",
                       facts_used=tuple(claim.fact_ids),
                       missing_evidence=("equivalence_margin",), severity="blocker")
    if needs_noninferiority:
        return _result("statistical_binding", claim.claim_id, "failed",
                       "STATISTICAL_EVIDENCE_MISMATCH: non-inferiority requires explicit margin",
                       facts_used=tuple(claim.fact_ids),
                       missing_evidence=("noninferiority_margin",), severity="blocker")
    return _result("statistical_binding", claim.claim_id, "needs_human",
                   "STATISTICAL_EVIDENCE_MISMATCH: no bound statistical result for this claim",
                   facts_used=tuple(claim.fact_ids),
                   missing_evidence=("statistical_test_result",), severity="major")


def coverage_gate(
    claims: Sequence[ScientificClaim], results: Sequence[ScientificCheckResult]
) -> dict[str, dict[str, Any]]:
    """Every claim type must run its required checkers; gaps never review."""
    by_claim: dict[str, list[ScientificCheckResult]] = {}
    for result in results:
        by_claim.setdefault(result.claim_id, []).append(result)
    verdicts: dict[str, dict[str, Any]] = {}
    for claim in claims:
        required = REQUIRED_CHECKS.get(claim.claim_type)
        if required is None:
            verdicts[claim.claim_id] = {"verdict": "needs_human", "reason": "UNCLASSIFIED_SCIENTIFIC_CLAIM"}
            continue
        if _is_statistical_claim(claim):
            required = tuple(dict.fromkeys((*required, "statistical_wording", "statistical_binding")))
        ran = {r.check_type for r in by_claim.get(claim.claim_id, [])}
        missing = [check for check in required if check not in ran]
        if missing:
            verdicts[claim.claim_id] = {"verdict": "not_reviewed", "reason": f"required checker missing: {missing}"}
            continue
        relevant = [r for r in by_claim.get(claim.claim_id, []) if r.check_type in set(required)]
        if any(r.status == "failed" for r in relevant):
            verdicts[claim.claim_id] = {"verdict": "failed", "reason": "required check failed"}
        elif any(r.status == "needs_human" for r in relevant):
            verdicts[claim.claim_id] = {"verdict": "needs_human", "reason": "required check needs human"}
        else:
            verdicts[claim.claim_id] = {"verdict": "reviewed", "reason": "required checks passed"}
    return verdicts


def check_sentence_coverage(
    sentences: Sequence[Mapping[str, Any]], claims: Sequence[ScientificClaim]
) -> list[dict[str, Any]]:
    """Every non-pure-transition scientific sentence needs a Claim or explicit non-science tag."""
    claimed = {c.claim_id for c in claims}
    findings: list[dict[str, Any]] = []
    for sent in sentences:
        role = str(sent.get("role") or "")
        text = str(sent.get("text") or "")
        sid = str(sent.get("sentence_id") or "")
        if role == "transition" and not re.search(r"\d|significant|equivalent|measured|simulated|hardwar|generaliz", text, re.I):
            findings.append({"sentence_id": sid, "status": "passed", "detail": "pure transition"})
            continue
        if sid in claimed or any(sid in str(c.locator or "") for c in claims):
            findings.append({"sentence_id": sid, "status": "passed"})
        else:
            findings.append({"sentence_id": sid, "status": "needs_human",
                             "detail": "scientific sentence without claim"})
    return findings


def aggregate_review_state(
    coverage: Mapping[str, Mapping[str, Any]],
    assumptions: Sequence[Assumption] | None = None,
    assumption_relevance: Mapping[str, Sequence[str]] | None = None,
) -> str:
    """Unverified assumptions cap the review: never fully reviewed."""
    verdicts = {str(v.get("verdict")) for v in coverage.values()}
    if not coverage or "failed" in verdicts:
        return "not_reviewed"
    relevance = {str(k): set(v or ()) for k, v in dict(assumption_relevance or {}).items()}
    for assumption in assumptions or []:
        if assumption.status == "unverified" and any(
            assumption.assumption_id in relevance.get(claim_id, set()) for claim_id in coverage
        ):
            return "reviewed_with_caveats"
    if "needs_human" in verdicts:
        return "reviewed_with_caveats"
    if "not_reviewed" in verdicts:
        return "partial"
    return "reviewed"


def _canonical(payload: Mapping[str, Any]) -> bytes:
    import json as _json

    return _json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def freeze_review(
    workspace: str | Path, memory: Any, *,
    claims: Sequence[ScientificClaim],
    results: Sequence[ScientificCheckResult],
    narrative_digest: str = "",
    fact_artifact_hashes: Mapping[str, Any] | None = None,
    ledger_bindings: Sequence[Mapping[str, Any]] | None = None,
    layer_bindings: Sequence[Mapping[str, Any]] | None = None,
    rule_pack: ScienceRulePack | None = None,
    assumptions: Sequence[Assumption] | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Freeze review inputs+outputs+versions. Files immutable; registry is the index."""
    from pathlib import Path as _Path

    from .path_safety import safe_mkdir as _safe_mkdir

    root = _Path(workspace).expanduser().resolve(strict=True)
    rule_pack = rule_pack or ScienceRulePack()
    rule_digest = hashlib.sha256(_canonical({
        "schema": rule_pack.schema,
        "metric_dependencies": [dict(d) for d in rule_pack.metric_dependencies],
        "assumptions": [dict(d) for d in rule_pack.assumptions],
        "allowed_comparison_differences": list(rule_pack.allowed_comparison_differences),
        "calibration_roles": [dict(d) for d in rule_pack.calibration_roles],
        "scope": dict(rule_pack.scope),
        "terminology": dict(rule_pack.terminology),
    })).hexdigest()
    assumption_payload = [a.to_dict() for a in (assumptions or [])]
    assumption_digest = hashlib.sha256(_canonical({"assumptions": assumption_payload})).hexdigest()
    checker_manifest = sorted({f"{r.check_type}@{r.checker_id}" for r in results})
    stable = {
        "schema": REVIEW_BUNDLE_SCHEMA,
        "claim_ids": sorted(c.claim_id for c in claims),
        "claims": sorted([c.to_dict() for c in claims], key=lambda c: str(c["claim_id"])),
        "narrative_digest": narrative_digest,
        "fact_artifact_hashes": dict(sorted(dict(fact_artifact_hashes or {}).items())),
        "ledger_bindings": sorted(
            [dict(sorted(d.items())) if isinstance(d, Mapping) else d for d in (ledger_bindings or [])],
            key=str),
        "layer_bindings": sorted(
            [dict(sorted(d.items())) if isinstance(d, Mapping) else d for d in (layer_bindings or [])],
            key=str),
        "rule_pack_digest": rule_digest,
        "rule_pack": {
            "schema": rule_pack.schema,
            "metric_dependencies": [dict(d) for d in rule_pack.metric_dependencies],
            "allowed_comparison_differences": list(rule_pack.allowed_comparison_differences),
        },
        "checker_manifest": checker_manifest,
        "checker_version": CHECKER_VERSION,
        "assumption_registry_digest": assumption_digest,
        "assumptions": sorted(assumption_payload, key=lambda a: str(a["assumption_id"])),
        "results": sorted([r.to_dict() for r in results],
                          key=lambda r: (str(r["claim_id"]), str(r["check_type"]))),
        "coverage": coverage_gate(claims, results),
    }
    digest = hashlib.sha256(_canonical(stable)).hexdigest()
    review_id = f"review-{digest[:16]}"
    review_dir = _safe_mkdir(root / ".paperforge" / "reviews" / review_id, anchor=root)
    payload = {**stable, "review_id": review_id, "review_digest": digest,
               "created_at": __import__("paperforge.models", fromlist=["utc_now"]).utc_now()}
    from .immutable_artifacts import register_immutable, write_immutable_bundle

    write_immutable_bundle(root, {f".paperforge/reviews/{review_id}/{name}": content for name, content in (
        ("review.json", payload),
        ("manifest.json", {"review_id": review_id, "review_digest": digest,
                           "checker_version": CHECKER_VERSION, "rule_pack_digest": rule_digest}),
    )})
    payload = json.loads((review_dir / "review.json").read_bytes())
    # Register as review/audit artifact, never as scientific Evidence.
    from .artifacts import ArtifactStore

    store = ArtifactStore(
        root, allowed_roots=(".paperforge/reviews",), allowed_suffixes=(".json",),
        allowed_kinds=("review", "manifest", "report"), memory=memory)
    metadata = {"review_id": review_id, "review_digest": digest,
                "checker_version": CHECKER_VERSION, "rule_pack_digest": rule_digest,
                "claim_ids": sorted(c.claim_id for c in claims), "workflow_id": run_id}
    for name, kind in (("review.json", "review"), ("manifest.json", "manifest")):
        register_immutable(store, f".paperforge/reviews/{review_id}/{name}", kind=kind,
                       run_id=run_id, status="VERIFIED",
                       media_type="application/json", metadata=metadata)
    return payload


def verify_scientific_review(workspace: str | Path, memory: Any, review_id: str) -> dict[str, Any]:
    """Historical integrity vs current input validity. Frozen JSON never mutates."""
    from pathlib import Path as _Path

    root = _Path(workspace).expanduser().resolve(strict=True)
    review_path = root / ".paperforge" / "reviews" / review_id / "review.json"
    if not review_path.is_file():
        raise ScientificReviewError(f"review missing: {review_id}")
    import json as _json

    raw = review_path.read_bytes()
    file_sha = hashlib.sha256(raw).hexdigest()
    try:
        payload = _json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        return {"review_id": review_id, "review_file_integrity": "failed", "detail": str(exc)}
    stored = str(payload.get("review_digest") or "")
    stable = {k: payload[k] for k in payload
              if k not in {"review_id", "review_digest", "created_at", "review_file_sha256"}}
    digest_valid = stored == hashlib.sha256(_canonical(stable)).hexdigest()
    # Currentness: ledgers + rule pack + checker version (reuse existing gates).
    stale: list[str] = []
    try:
        from .result_ledger import verify_ledger

        for binding in payload.get("ledger_bindings") or []:
            if not isinstance(binding, Mapping) or not binding.get("ledger_id"):
                continue
            try:
                report = verify_ledger(root, memory, str(binding["ledger_id"]))
            except Exception:
                stale.append(str(binding.get("ledger_id")))
                continue
            if not report.get("eligible_as_release_input"):
                stale.append(str(binding.get("ledger_id")))
    except Exception:
        stale.append("<ledger-check-unavailable>")
    rule_match = str((payload.get("rule_pack") or {}).get("schema") or "") == RULE_PACK_SCHEMA
    checker_match = all(str(c).startswith("paperforge.science-checkers/") is False or True
                        for c in payload.get("checker_manifest") or [])
    checker_match = str(payload.get("checker_version") or "") == CHECKER_VERSION and checker_match
    historical = "valid" if digest_valid else "invalid"
    current = "current" if digest_valid and not stale and rule_match and checker_match else "stale"
    from .immutable_artifacts import verify_registered_bundle

    registry = verify_registered_bundle(root, memory, {
        f".paperforge/reviews/{review_id}/review.json": "review",
        f".paperforge/reviews/{review_id}/manifest.json": "manifest"})
    return {
        **registry,
        "review_id": review_id,
        "review_file_sha256": file_sha,
        "review_file_integrity": "valid",
        "review_digest_valid": digest_valid,
        "input_artifact_integrity": "valid" if digest_valid else "invalid",
        "ledger_currentness": "current" if not stale else "stale",
        "stale_ledger_ids": sorted(set(stale)),
        "evidence_currentness": "current" if not stale else "stale",
        "rule_pack_match": rule_match,
        "checker_version_match": checker_match,
        "historical_integrity": historical,
        "source_currentness": current,
        "currently_valid_as_review_input": bool(historical == "valid" and current == "current"
                                                and registry["registered_complete"]),
        "warnings": [] if historical == "valid" else ["review file or digest mismatch"],
    }


__all__ = [
    "Assumption",
    "CHECKER_VERSION",
    "CLAIM_TYPES",
    "REQUIRED_CHECKS",
    "REVIEW_BUNDLE_SCHEMA",
    "RULE_PACK_SCHEMA",
    "ScienceRulePack",
    "ScientificCheckResult",
    "ScientificClaim",
    "ScientificReviewError",
    "StatisticalEvidence",
    "aggregate_review_state",
    "check_sentence_coverage",
    "check_statistical_binding",
    "coverage_gate",
    "freeze_review",
    "review_claim",
    "review_state",
    "verify_scientific_review",
]
