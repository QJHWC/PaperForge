from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .latex_safety import FORBIDDEN_LATEX as _FORBIDDEN_LATEX

NARRATIVE_SPEC_SCHEMA = "paperforge.grounded-narrative-spec/v1"
NARRATIVE_DRAFT_SCHEMA = "paperforge.grounded-narrative-draft/v1"
NARRATIVE_POLICY_VERSION = "paperforge.narrative-policy/v2"

_SENTENCE_ROLES = (
    "metric_report", "comparison", "protocol_statement",
    "limitation", "provenance_statement", "transition",
)

_STATISTICAL_FLAGS = (
    "significant", "statistically significant", "equivalent", "non-inferior",
    "robust", "stable", "consistent", "generalizable",
)

_STRONG_RANKING = ("best overall", "superior", "dominates", "state-of-the-art", "best")

_LAYER_ALLOWED = {
    "simulated": ("simulated", "proxy", "under the simulation profile", "simulation profile"),
    "remapped": ("remapped", "proxy", "under the disclosed transfer", "transfer rule", "remapping rule"),
    "measured": ("measured", "on-device measurement", "device measurement"),
}

_LAYER_FORBIDDEN = {
    "simulated": ("measured on device", "measured on raspberry", "physical hardware result",
                  "physical device measurement",
                  "hardware validated", "on-device latency", "direct device measurement"),
    "remapped": ("physical device measurement", "direct device measurement", "hardware validated"),
    "measured": ("hardware validated", "robust", "generalizable", "production ready"),
}


class NarrativeError(RuntimeError):
    pass


class NarrativeGroundingError(NarrativeError):
    pass


@dataclass(frozen=True)
class GroundedNarrativeSpec:
    purpose: str = "results_description"
    target_block_ref: str = ""
    allowed_fact_artifact_ids: tuple[str, ...] = ()
    allowed_fact_ids: tuple[str, ...] = ()
    allowed_protocol_fact_ids: tuple[str, ...] = ()
    allowed_provenance_fact_ids: tuple[str, ...] = ()
    allowed_evidence_layers: tuple[str, ...] = ()
    allowed_citation_keys: tuple[str, ...] = ()
    writing_instructions: str = ""
    language: str = "en"
    max_paragraphs: int = 4
    max_sentences: int = 20
    narrative_policy_version: str = NARRATIVE_POLICY_VERSION

    def __post_init__(self) -> None:
        if not self.allowed_fact_ids and not self.allowed_protocol_fact_ids:
            raise NarrativeError("spec must allowlist at least one fact")
        if self.max_paragraphs <= 0 or self.max_sentences <= 0:
            raise NarrativeError("paragraph/sentence budget must be positive")
        for key in ("allowed_fact_artifact_ids", "allowed_fact_ids", "allowed_protocol_fact_ids",
                    "allowed_provenance_fact_ids", "allowed_evidence_layers", "allowed_citation_keys"):
            object.__setattr__(self, key, tuple(getattr(self, key) or ()))

    def to_canonical(self) -> dict[str, Any]:
        return {
            "schema": NARRATIVE_SPEC_SCHEMA,
            "purpose": self.purpose,
            "target_block_ref": self.target_block_ref,
            "allowed_fact_artifact_ids": sorted(self.allowed_fact_artifact_ids),
            "allowed_fact_ids": sorted(self.allowed_fact_ids),
            "allowed_protocol_fact_ids": sorted(self.allowed_protocol_fact_ids),
            "allowed_provenance_fact_ids": sorted(self.allowed_provenance_fact_ids),
            "allowed_evidence_layers": sorted(self.allowed_evidence_layers),
            "allowed_citation_keys": sorted(self.allowed_citation_keys),
            "writing_instructions": self.writing_instructions,
            "language": self.language,
            "max_paragraphs": self.max_paragraphs,
            "max_sentences": self.max_sentences,
            "narrative_policy_version": self.narrative_policy_version,
        }

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(
            self.to_canonical(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class NarrativeSentence:
    sentence_id: str
    text: str
    role: str = "metric_report"
    fact_ids: tuple[str, ...] = ()
    protocol_fact_ids: tuple[str, ...] = ()
    provenance_fact_ids: tuple[str, ...] = ()
    citation_keys: tuple[str, ...] = ()
    evidence_layer_ids: tuple[str, ...] = ()
    claim_types: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not str(self.sentence_id or "").strip() or not str(self.text or "").strip():
            raise NarrativeError("sentence_id/text are required")
        if self.role not in _SENTENCE_ROLES:
            raise NarrativeError(f"unknown role: {self.role}")
        for key in ("fact_ids", "protocol_fact_ids", "provenance_fact_ids", "citation_keys",
                    "evidence_layer_ids", "claim_types"):
            object.__setattr__(self, key, tuple(getattr(self, key) or ()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "sentence_id": self.sentence_id, "text": self.text, "role": self.role,
            "fact_ids": list(self.fact_ids), "protocol_fact_ids": list(self.protocol_fact_ids),
            "provenance_fact_ids": list(self.provenance_fact_ids),
            "citation_keys": list(self.citation_keys),
            "evidence_layer_ids": list(self.evidence_layer_ids),
            "claim_types": list(self.claim_types),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> NarrativeSentence:
        return cls(
            sentence_id=str(payload["sentence_id"]), text=str(payload["text"]),
            role=str(payload.get("role") or "metric_report"),
            fact_ids=tuple(payload.get("fact_ids") or ()),
            protocol_fact_ids=tuple(payload.get("protocol_fact_ids") or ()),
            provenance_fact_ids=tuple(payload.get("provenance_fact_ids") or ()),
            citation_keys=tuple(payload.get("citation_keys") or ()),
            evidence_layer_ids=tuple(payload.get("evidence_layer_ids") or ()),
            claim_types=tuple(payload.get("claim_types") or ()),
        )


def parse_model_output(raw: Any) -> dict[str, Any]:
    """Parse structured model output fail-closed (invalid/truncated/unknown)."""
    from .provider import GenerationResult, coerce_generation_result

    if isinstance(raw, GenerationResult):
        if raw.truncated:
            raise NarrativeGroundingError("GENERATION_TRUNCATED: refusing narrative")
        text = raw.text
    elif isinstance(raw, Mapping) and "text" in raw:
        generation = coerce_generation_result(dict(raw))
        if generation.truncated:
            raise NarrativeGroundingError("GENERATION_TRUNCATED: refusing narrative")
        text = generation.text
    elif isinstance(raw, str):
        text = raw
    else:
        raise NarrativeGroundingError("unsupported model output type")
    try:
        payload = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise NarrativeGroundingError(f"model output is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("paragraphs"), list):
        raise NarrativeGroundingError("model output misses paragraphs[]")
    return payload


def draft_from_model_output(raw: Any) -> list[list[NarrativeSentence]]:
    payload = parse_model_output(raw)
    paragraphs: list[list[NarrativeSentence]] = []
    for para in payload["paragraphs"]:
        sentences = para.get("sentences") if isinstance(para, dict) else None
        if not isinstance(sentences, list) or not sentences:
            raise NarrativeGroundingError("each paragraph needs sentences[]")
        paragraphs.append([NarrativeSentence.from_dict(s) for s in sentences])
    return paragraphs


def _numbers(text: str) -> list[str]:
    return [m.group(1) for m in re.finditer(r"(?<!\w)(-?\d+(?:\.\d+)?)(?!\w)", text)]


def _facts_by_id(facts: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    return {str(f.get("fact_id")): f for f in facts if isinstance(f, Mapping) and f.get("fact_id")}


def validate_narrative(
    spec: GroundedNarrativeSpec,
    paragraphs: Sequence[Sequence[NarrativeSentence]],
    *,
    facts: Sequence[Mapping[str, Any]],
    protocol_facts: Sequence[Mapping[str, Any]] | None = None,
    reference_excerpts_only: bool = False,
) -> dict[str, Any]:
    """Deterministic grounding validation. No file writes, no approval."""
    fact_map = _facts_by_id(facts)
    protocol_map = _facts_by_id(protocol_facts or [])
    total = sum(len(p) for p in paragraphs)
    if sum(1 for _ in paragraphs) > spec.max_paragraphs or total > spec.max_sentences:
        raise NarrativeGroundingError("narrative exceeds paragraph/sentence budget")
    checks: list[dict[str, Any]] = []
    needs_human: list[str] = []
    allowed_facts, allowed_proto = set(spec.allowed_fact_ids), set(spec.allowed_protocol_fact_ids)
    for para in paragraphs:
        for sent in para:
            # Allowlist + unknown IDs.
            for fid in (*sent.fact_ids, *sent.protocol_fact_ids):
                if fid not in allowed_facts | allowed_proto:
                    raise NarrativeGroundingError(f"fact outside allowlist: {fid}")
                if fid in sent.fact_ids and fid not in fact_map:
                    raise NarrativeGroundingError(f"unknown fact ID: {fid}")
                if fid in sent.protocol_fact_ids and fid not in protocol_map:
                    raise NarrativeGroundingError(f"unknown protocol fact ID: {fid}")
            for key in sent.citation_keys:
                if key not in set(spec.allowed_citation_keys):
                    raise NarrativeGroundingError(f"citation outside allowlist: {key}")
            # Layer allowlist.
            for layer in sent.evidence_layer_ids:
                if layer not in set(spec.allowed_evidence_layers) and spec.allowed_evidence_layers:
                    raise NarrativeGroundingError(f"layer outside allowlist: {layer}")
            # Transition purity.
            if sent.role == "transition":
                if _numbers(sent.text) or re.search(
                    r"\b(higher|lower|faster|slower|improv|reduc|increas|outperform|significant|hardware|generaliz|measured|simulated|remapped|\d)", sent.text, re.I
                ):
                    raise NarrativeGroundingError(f"transition carries scientific content: {sent.sentence_id}")
                checks.append({"sentence_id": sent.sentence_id, "status": "passed", "detail": "pure transition"})
                continue
            # Every scientific number must match a bound fact (no model arithmetic).
            bound_values = [str(fact_map[f]["value"]) for f in sent.fact_ids if f in fact_map]
            bound_values += [str(protocol_map[f]["value"]) for f in sent.protocol_fact_ids if f in protocol_map]
            for number in _numbers(sent.text):
                # Structural refs (Table/Fig/Eq/Section + year-like integers) are not facts.
                if re.search(rf"(Table|Fig\.?|Eq\.?|Section)\s*{re.escape(number)}", sent.text, re.I):
                    continue
                matched = False
                for bound in bound_values:
                    try:
                        if Decimal(number) == Decimal(str(bound)):
                            matched = True
                            break
                    except (InvalidOperation, ValueError):
                        continue
                if not matched:
                    # Protocol numbers (epochs/seeds/px) must match protocol facts, not metric facts.
                    raise NarrativeGroundingError(
                        f"UNBOUND_SCIENTIFIC_VALUE in {sent.sentence_id}: {number!r}")
            # Comparison sentences need an explicit bound value covering the claim.
            if sent.role == "comparison" and not sent.fact_ids:
                raise NarrativeGroundingError(f"comparison without fact: {sent.sentence_id}")
            # Layer wording + collapse guard.
            kinds = [str(fact_map[f].get("measurement_kind") or fact_map[f].get("kind") or "") for f in sent.fact_ids if f in fact_map]
            lowered = sent.text.lower()
            for kind in set(kinds):
                for forbidden in _LAYER_FORBIDDEN.get(kind, ()):
                    if forbidden in lowered:
                        # Measured hardware-validated style claims always need human review.
                        if kind == "measured" and forbidden in ("hardware validated", "robust", "generalizable", "production ready"):
                            needs_human.append(f"{sent.sentence_id}: {forbidden}")
                        else:
                            raise NarrativeGroundingError(
                                f"layer wording violation in {sent.sentence_id}: {forbidden!r} for {kind}")
            if len(set(kinds)) > 1:
                if len(sent.evidence_layer_ids) < len(set(kinds)):
                    raise NarrativeGroundingError(f"cross-layer collapse in {sent.sentence_id}")
                if not re.search(r"simulat|remap|measur|proxy|layer", lowered):
                    raise NarrativeGroundingError(f"cross-layer wording missing in {sent.sentence_id}")
            # Qualifier consistency (model/dataset/device/size when facts declare them).
            for fid in sent.fact_ids:
                qualifiers = dict((fact_map.get(fid) or {}).get("qualifiers") or {})
                identity = dict((fact_map.get(fid) or {}).get("run_identity") or {})
                expected = str(identity.get("model_id") or qualifiers.get("model_id") or "")
                if expected and not re.search(rf"(?<!\w){re.escape(expected)}(?!\w)", sent.text, re.I):
                    raise NarrativeGroundingError(
                        f"GROUNDING_MISMATCH model_id in {sent.sentence_id}: must name {expected!r}")
                if expected:
                    unit = str((fact_map.get(fid) or {}).get("unit") or "")
                    simple_report = (
                        rf"\s*(?:The\s+)?{re.escape(expected)}\s+"
                        rf"(?:measured(?:\s+latency\s+was)?|reached|recorded)\s+"
                        rf"[+-]?\d+(?:\.\d+)?(?:\s+{re.escape(unit)})?\.\s*"
                    )
                    if not re.fullmatch(simple_report, sent.text, re.I):
                        needs_human.append(f"{sent.sentence_id}: model/value subject attribution unresolved")
                text_identity = {
                    "input_size": re.search(r"(\d{3,4})\s*px", sent.text, re.I),
                    "device": re.search(r"raspberry|sim_rpi5|remapped|host", sent.text, re.I),
                }
                if text_identity["input_size"]:
                    claimed = text_identity["input_size"].group(1)
                    for fid2 in sent.fact_ids:
                        qual = dict((fact_map.get(fid2) or {}).get("qualifiers") or {})
                        if qual.get("input_size") and str(qual["input_size"]) != claimed:
                            raise NarrativeGroundingError(f"GROUNDING_MISMATCH input_size in {sent.sentence_id}")
            # Statistical / ranking flags.
            for word in ("significant", "statistically significant", "equivalent", "non-inferior"):
                if word in lowered:
                    needs_human.append(f"{sent.sentence_id}: {word}")
            for word in ("best overall", "superior", "dominates", "state-of-the-art", "best"):
                if word in lowered and sent.role != "transition":
                    raise NarrativeGroundingError(f"ranking claim without authorization: {sent.sentence_id}")
            # Reference excerpts never substitute experiment facts.
            if reference_excerpts_only and _numbers(sent.text):
                raise NarrativeGroundingError(f"reference-only numbers need experiment fact: {sent.sentence_id}")
            checks.append({"sentence_id": sent.sentence_id, "status": "passed"})
    overall = "needs_human" if needs_human else "passed"
    return {"checks": checks, "needs_human": sorted(set(needs_human)), "overall_status": overall,
            "sentence_count": total}


def render_narrative(paragraphs: Sequence[Sequence[NarrativeSentence]]) -> str:
    """Deterministic LaTeX-safe rendering. Numbers/citations come from structure."""
    rendered_paragraphs: list[str] = []
    for para in paragraphs:
        sentences: list[str] = []
        for sent in para:
            text = str(sent.text or "")
            if _FORBIDDEN_LATEX.search(text):
                raise NarrativeGroundingError(f"forbidden LaTeX in {sent.sentence_id}")
            if re.search(r"\\cite\{([^}]*)\}", text):
                cited = sorted({k.strip() for m in re.finditer(r"\\cite\{([^}]*)\}", text)
                                for k in m.group(1).split(",") if k.strip()})
                if sorted(sent.citation_keys) != cited:
                    raise NarrativeGroundingError(f"citation rendering mismatch: {sent.sentence_id}")
            else:
                for key in sent.citation_keys:
                    text += f" \\cite{{{key}}}"
            # Escape residual specials except the citations just ensured.
            Renaissance: list[str] = []
            parts = re.split(r"(\\cite\{[^}]*\})", text)
            for index, part in enumerate(parts):
                if index % 2 == 1:
                    Renaissance.append(part)
                else:
                    Renaissance.append(part.replace("&", "\\&").replace("%", "\\%").replace("$", "\\$")
                                     .replace("#", "\\#").replace("_", "\\_"))
            sentences.append("".join(Renaissance).strip())
        rendered_paragraphs.append(" ".join(sentences))
    return "\n\n".join(rendered_paragraphs)


def narrative_audit(
    spec: GroundedNarrativeSpec, paragraphs: Sequence[Sequence[NarrativeSentence]],
    validation: Mapping[str, Any], *, provider: str = "mock", model: str = "mock",
    request_sha256: str = "", response_sha256: str = "",
) -> dict[str, Any]:
    flat = [s.to_dict() for para in paragraphs for s in para]
    digest = hashlib.sha256(json.dumps(
        [s for s in flat], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {
        "schema": "paperforge.grounded-narrative-audit/v1",
        "spec_digest": spec.digest(),
        "narrative_policy_version": spec.narrative_policy_version,
        "narrative_digest": digest,
        "validation_digest": hashlib.sha256(json.dumps(
            dict(validation), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "sentence_bindings": flat,
        "provider": provider, "model": model,
        "request_sha256": request_sha256, "response_sha256": response_sha256,
        "note": "narrative artifact, not scientific evidence",
    }


def build_update_from_narrative(
    workspace: str | Path, memory: Any, *, spec: GroundedNarrativeSpec,
    paragraphs: Sequence[Sequence[NarrativeSentence]],
    facts: Sequence[Mapping[str, Any]], target_spec_kwargs: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate + render + hand to U2.4A. Never writes the target itself."""
    from .result_update import ResultUpdateSpec

    validation = validate_narrative(spec, paragraphs, facts=facts)
    if validation["overall_status"] == "failed":
        raise NarrativeGroundingError("narrative validation failed")
    rendered = render_narrative(paragraphs)
    audit = narrative_audit(spec, paragraphs, validation)
    target_spec = ResultUpdateSpec(**dict(target_spec_kwargs))
    # The rendered fragment is wrapped by the caller into a full protected block;
    # here we return everything U2.4A needs without bypassing its gates.
    return {"rendered": rendered, "validation": validation, "audit": audit,
            "target_spec": target_spec.to_canonical(),
            "facts_used": [str(f.get("fact_id")) for f in facts if str(f.get("fact_id")) in spec.allowed_fact_ids]}


__all__ = [
    "NARRATIVE_POLICY_VERSION",
    "GroundedNarrativeSpec",
    "NarrativeGroundingError",
    "NarrativeError",
    "NarrativeSentence",
    "build_update_from_narrative",
    "draft_from_model_output",
    "narrative_audit",
    "parse_model_output",
    "render_narrative",
    "validate_narrative",
]
