from __future__ import annotations

import difflib
import hashlib
import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .models import utc_now
from .path_safety import safe_mkdir
from .protected_blocks import (
    PROTECTED_END,
    PROTECTED_START,
    ProtectedBlockViolation,
    extract_protected_blocks,
    protected_blocks_sha256,
)

UPDATE_SPEC_SCHEMA = "paperforge.result-update-spec/v1"
PROPOSAL_SCHEMA = "paperforge.result-update-proposal/v1"

_STRONG_CLAIM_PATTERNS = {
    "ranking": re.compile(r"\b(best|rank(?:ed|ing)?\s+(first|1st)|outperform(?:s|ed|ing)?\b)", re.I),
    "superiority": re.compile(r"\b(superior|outperform|state-of-the-art|SOTA)\b", re.I),
    "statistical": re.compile(r"\b(significant(?:ly)?|statistically\s+significant|p\s*<\s*0\.05)\b", re.I),
    "equivalence": re.compile(r"\b(equivalent|equivalence|no\s+difference)\b", re.I),
    "hardware": re.compile(r"\b(validated on hardware|hardware validated|real device measurement)\b", re.I),
    "generalization": re.compile(r"\b(generaliz(?:e|able)|robust|real-time)\b", re.I),
}

# Protocol values that are not scientific measurements.
_PROTOCOL_PATTERNS = [
    re.compile(r"\b(Table|Figure|Fig\.|Eq\.|Equation|Section|Sec\.)\s*\d+", re.I),
    re.compile(r"\b\d+\s*(epochs?|seeds?|folds?|runs?)\b", re.I),
    re.compile(r"\bbatch(?:-size)?\s*\d+\b", re.I),
    re.compile(r"\\(?:label|ref|cite)[^}]*"),
]


class ResultUpdateError(RuntimeError):
    pass


class ResultUpdateValidationError(ResultUpdateError):
    pass


class ResultUpdateApprovalError(ResultUpdateError):
    pass


@dataclass(frozen=True)
class ProtocolValueBinding:
    name: str
    canonical_value: str
    unit: str = ""
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ResultUpdateSpec:
    target_path: str
    target_source_sha256: str
    protected_block_index: int = 0
    old_block_sha256: str = ""
    allowed_fact_artifact_ids: tuple[str, ...] = ()
    allowed_table_artifact_ids: tuple[str, ...] = ()
    allowed_figure_artifact_ids: tuple[str, ...] = ()
    ledger_bindings: tuple[Mapping[str, Any], ...] = ()
    allowed_fact_ids: tuple[str, ...] = ()
    allowed_protocol_values: tuple[Mapping[str, Any], ...] = ()
    allowed_citation_keys: tuple[str, ...] = ()
    update_mode: str = "replace_block"
    update_scope: str = ""

    def __post_init__(self) -> None:
        if not str(self.target_path or "").strip() or ".." in Path(str(self.target_path)).parts:
            raise ResultUpdateError("target_path must be a single workspace-relative file")
        if "*" in str(self.target_path) or "?" in str(self.target_path):
            raise ResultUpdateError("wildcard targets are forbidden")
        if self.update_mode != "replace_block":
            raise ResultUpdateError("v1 only supports replace_block")
        if self.protected_block_index < 0:
            raise ResultUpdateError("protected_block_index must be non-negative")
        for key in ("allowed_fact_artifact_ids", "allowed_table_artifact_ids", "allowed_figure_artifact_ids",
                    "allowed_fact_ids", "allowed_citation_keys", "ledger_bindings"):
            value = getattr(self, key)
            object.__setattr__(self, key, tuple(value or ()))
        protocols = []
        for entry in self.allowed_protocol_values or ():
            if not isinstance(entry, Mapping):
                raise ResultUpdateError("protocol values must be mappings")
            protocols.append(dict(entry))
        object.__setattr__(self, "allowed_protocol_values", tuple(protocols))

    def to_canonical(self) -> dict[str, Any]:
        return {
            "schema": UPDATE_SPEC_SCHEMA,
            "target_path": self.target_path,
            "target_source_sha256": self.target_source_sha256,
            "protected_block_index": self.protected_block_index,
            "old_block_sha256": self.old_block_sha256,
            "allowed_fact_artifact_ids": sorted(self.allowed_fact_artifact_ids),
            "allowed_table_artifact_ids": sorted(self.allowed_table_artifact_ids),
            "allowed_figure_artifact_ids": sorted(self.allowed_figure_artifact_ids),
            "ledger_bindings": [dict(sorted(d.items())) for d in self.ledger_bindings],
            "allowed_fact_ids": sorted(self.allowed_fact_ids),
            "allowed_protocol_values": [dict(sorted(d.items())) for d in self.allowed_protocol_values],
            "allowed_citation_keys": sorted(self.allowed_citation_keys),
            "update_mode": self.update_mode,
            "update_scope": self.update_scope,
        }

    def digest(self) -> str:
        return hashlib.sha256(
            json.dumps(self.to_canonical(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


def _canonical(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _sha256_str(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _resolve_target(root: Path, rel: str) -> Path:
    candidate = (root / rel).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ResultUpdateError("target leaves workspace") from exc
    if candidate.is_symlink() or not candidate.is_file():
        raise ResultUpdateError("target must be a regular file")
    return candidate


def _block_hash(block: str) -> str:
    return hashlib.sha256(block.encode("utf-8")).hexdigest()


def _extract_citations(text: str) -> list[str]:
    return sorted({key.strip() for group in re.findall(r"\\cite[a-zA-Z*]*\s*(?:\[[^\]]*\]\s*){0,2}\{([^{}]+)\}", text)
                   for key in group.split(",") if key.strip()})


def _classify_candidates(text: str) -> list[dict[str, Any]]:
    """Split numbers into structural / metric / protocol values.

    Structural (Table III, Fig. 3, Eq. 7, Section IV, [24]): no binding.
    Metric (mAP/latency/FPS/energy/delta/std): requires Fact binding.
    Protocol (896 px, 300 epochs, 3 seeds, batch 1, c=1, 7.5 ms period):
    requires ProtocolValueBinding from frozen provenance, never free text.
    """
    structural_spans: list[tuple[int, int]] = []
    for pattern in (
        re.compile(r"\b(Table|Figure|Fig\.|Eq\.|Equation|Section|Sec\.)\s*\d+[A-Z]?", re.I),
        re.compile(r"\[\d+(?:\s*,\s*\d+)*\]"),
        re.compile(r"\\(?:label|ref|cite)[^}]*"),
    ):
        for match in pattern.finditer(text):
            structural_spans.append((match.start(), match.end()))

    def _in_structural(start: int, end: int) -> bool:
        return any(start < span_end and end > span_start for span_start, span_end in structural_spans)

    out: list[dict[str, Any]] = []
    pattern = re.compile(
        r"(?<!\w)(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>ms|s|mJ|J|FPS|fps|%|mAP(?:50[-–]95)?|px|epochs?|seeds?|folds?|runs?)?"
        r"(?P<tail>\s*(?:px|epochs?|seeds?|x\b))?",
        re.I,
    )
    for match in pattern.finditer(text):
        number = match.group("number")
        unit = (match.group("unit") or "").strip()
        tail = (match.group("tail") or "").strip().lower()
        if tail and not unit:
            unit = tail
        start, end = match.start(), match.end()
        if _in_structural(start, end):
            out.append({"text": match.group(0), "number": number, "unit": unit, "kind": "structural", "start": start})
            continue
        lowered_unit = unit.lower()
        # Protocol units/keywords (explicit, never silently ignored).
        if lowered_unit in {"px", "epoch", "epochs", "seed", "seeds", "fold", "folds", "run", "runs", "x"} or re.search(
            r"\b(epochs?|seeds?|batch|px|c\s*=)\b", match.group(0), re.I
        ):
            out.append({"text": match.group(0), "number": number, "unit": unit, "kind": "protocol", "start": start})
            continue
        if lowered_unit in {"ms", "s", "mj", "j", "fps", "%"} or "map" in lowered_unit or "." in number:
            # Metric-like values (mAP/latency/FPS/energy/delta/std).
            if not unit and len(number) < 4:
                out.append({"text": match.group(0), "number": number, "unit": unit, "kind": "structural", "start": start})
                continue
            out.append({"text": match.group(0), "number": number, "unit": unit, "kind": "metric", "start": start})
            continue
        out.append({"text": match.group(0), "number": number, "unit": unit, "kind": "structural", "start": start})
    return out


def _scientific_candidates(text: str) -> list[dict[str, Any]]:
    """Legacy helper: metric candidates only (protocol handled separately)."""
    return [c for c in _classify_candidates(text) if c["kind"] == "metric"]


def _artifact_directories(root: Path, spec: ResultUpdateSpec) -> dict[str, Path]:
    mapping: dict[str, Path] = {}
    for artifact_id in spec.allowed_fact_artifact_ids:
        mapping[f"fact:{artifact_id}"] = root / "artifacts" / "facts" / artifact_id
    for artifact_id in spec.allowed_table_artifact_ids:
        mapping[f"table:{artifact_id}"] = root / "artifacts" / "tables" / artifact_id
    for artifact_id in spec.allowed_figure_artifact_ids:
        mapping[f"figure:{artifact_id}"] = root / "artifacts" / "figures" / artifact_id
    return mapping


def _collect_artifact_hashes(root: Path, spec: ResultUpdateSpec) -> dict[str, dict[str, str]]:
    """Hash every real file byte under bound artifacts (ID alone never binds)."""
    collected: dict[str, dict[str, str]] = {}
    for key, directory in _artifact_directories(root, spec).items():
        if not directory.is_dir():
            raise ResultUpdateValidationError(f"bound artifact missing: {key}")
        files: dict[str, str] = {}
        for child in sorted(directory.rglob("*")):
            if child.is_symlink() or not child.is_file():
                continue
            rel = child.relative_to(directory).as_posix()
            files[rel] = hashlib.sha256(child.read_bytes()).hexdigest()
        if not files:
            raise ResultUpdateValidationError(f"bound artifact has no files: {key}")
        collected[key] = files
    return collected


def verify_bound_artifact_integrity(
    workspace: str | Path, memory: Any, spec: ResultUpdateSpec,
    *, expected: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Verify real file bytes + ArtifactStore records + manifest consistency."""
    root = Path(workspace).expanduser().resolve(strict=True)
    current = _collect_artifact_hashes(root, spec)
    results: list[dict[str, Any]] = []
    with memory.connect() as db:
        for key, files in current.items():
            for rel, actual in files.items():
                artifact_id = key.split(":", 1)[1]
                kind_dir = {"fact:": "facts", "table:": "tables", "figure:": "figures"}[key.split(":")[0] + ":"]
                store_rel = f"artifacts/{kind_dir}/{artifact_id}/{rel}"
                row = db.execute("SELECT * FROM artifacts WHERE path=?", (store_rel,)).fetchone()
                record_valid = bool(row) and str(row["sha256"]) == actual
                expected_sha = None
                if expected:
                    expected_sha = ((expected.get(key) or {}).get(rel)
                                    if isinstance(expected.get(key), dict) else None)
                results.append({
                    "artifact_id": artifact_id, "file": rel,
                    "expected_sha256": expected_sha, "actual_sha256": actual,
                    "integrity_valid": (expected_sha is None or expected_sha == actual),
                    "artifact_store_record_valid": record_valid,
                })
    failures = [r for r in results if not r["integrity_valid"] or not r["artifact_store_record_valid"]]
    if failures:
        raise ResultUpdateApprovalError(
            "bound artifact bytes changed: " + "; ".join(
                f"{f['artifact_id']}/{f['file']}" for f in failures[:3])
        )
    return results


def _semantic_diff(old: str, new: str) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for claim, pattern in _STRONG_CLAIM_PATTERNS.items():
        if not pattern.search(old) and pattern.search(new):
            findings.append({"claim": claim, "status": "needs_human",
                             "detail": f"new {claim} wording requires human review (U3 pending)"})
    return findings


def _verify_ledger_bindings(workspace: str | Path, memory: Any, bindings: Sequence[Mapping[str, Any]]) -> None:
    """Require bound ledgers to stay eligible; any stale/invalid blocks approval/apply."""
    if not bindings:
        return
    from .result_ledger import verify_ledger

    root = Path(workspace).expanduser().resolve(strict=True)
    for binding in bindings:
        if not isinstance(binding, Mapping) or not binding.get("ledger_id"):
            continue
        ledger_id = str(binding["ledger_id"])
        try:
            report = verify_ledger(root, memory, ledger_id)
        except Exception as exc:
            raise ResultUpdateApprovalError(f"bound ledger unavailable: {ledger_id}: {exc}") from exc
        expected_digest = str(binding.get("ledger_digest") or "")
        if expected_digest and expected_digest != str(report.get("ledger_id") and binding.get("ledger_digest")):
            pass
        # Compare digest when the binding declares one.
        try:
            from .result_ledger import _ledger_paths
            import json as _json

            ledger_payload = _json.loads((root / ".paperforge" / "ledgers" / ledger_id / "ledger.json").read_bytes().decode())
            if expected_digest and ledger_payload.get("ledger_digest") != expected_digest:
                raise ResultUpdateApprovalError(f"bound ledger digest changed: {ledger_id}")
        except (OSError, ValueError) as exc:
            raise ResultUpdateApprovalError(f"bound ledger unreadable: {ledger_id}: {exc}") from exc
        if not report.get("eligible_as_release_input"):
            raise ResultUpdateApprovalError(f"bound ledger not currently eligible: {ledger_id}")


def _verify_proposal_artifacts(
    workspace: str | Path, memory: Any, proposal: Mapping[str, Any],
    *, expected: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Recompute bound artifact bytes; approval binds bytes, never IDs alone."""
    root = Path(workspace).expanduser().resolve(strict=True)
    spec_data = None
    try:
        spec_data = (root / ".paperforge" / "result_updates" / str(proposal.get("proposal_id")) / "spec.json")
        spec_json = json.loads(spec_data.read_text(encoding="utf-8")) if spec_data.is_file() else {}
    except (OSError, ValueError):
        spec_json = {}
    spec = ResultUpdateSpec(
        target_path=str(spec_json.get("target_path") or "paper.tex"),
        target_source_sha256=str(spec_json.get("target_source_sha256") or ""),
        allowed_fact_artifact_ids=tuple(spec_json.get("allowed_fact_artifact_ids") or ()),
        allowed_table_artifact_ids=tuple(spec_json.get("allowed_table_artifact_ids") or ()),
        allowed_figure_artifact_ids=tuple(spec_json.get("allowed_figure_artifact_ids") or ()),
    )
    if not (spec.allowed_fact_artifact_ids or spec.allowed_table_artifact_ids or spec.allowed_figure_artifact_ids):
        return []
    results = verify_bound_artifact_integrity(root, memory, spec, expected=expected or proposal.get("artifact_hashes"))
    return results


def _ensure_update_tables(memory: Any) -> None:
    with memory.connect() as db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS result_updates (
                id TEXT PRIMARY KEY, spec_digest TEXT NOT NULL, proposal_digest TEXT NOT NULL,
                target_path TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL,
                proposal_json TEXT NOT NULL)"""
        )
        db.execute(
            """CREATE TABLE IF NOT EXISTS result_approvals (
                id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL, proposal_digest TEXT NOT NULL,
                target_source_sha256 TEXT NOT NULL, old_block_sha256 TEXT NOT NULL,
                validation_digest TEXT NOT NULL, scope TEXT NOT NULL,
                approver TEXT NOT NULL, approved_at TEXT NOT NULL,
                acknowledged_json TEXT NOT NULL, rationale TEXT NOT NULL, status TEXT NOT NULL)"""
        )
        db.execute(
            """CREATE TABLE IF NOT EXISTS result_baselines (
                target_path TEXT PRIMARY KEY, source_sha256 TEXT NOT NULL, block_sha256 TEXT NOT NULL,
                proposal_id TEXT NOT NULL, approval_id TEXT NOT NULL, applied_at TEXT NOT NULL)"""
        )


def _update_paths(root: Path, proposal_id: str) -> Path:
    return safe_mkdir(root / ".paperforge" / "result_updates" / proposal_id, anchor=root)


def build_proposal(
    workspace: str | Path, memory: Any, spec: ResultUpdateSpec,
    *, proposed_block: str, facts_used: Sequence[Mapping[str, Any]] | None = None,
    artifact_ids_used: Sequence[str] | None = None, citations_used: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Create a proposal without touching the target file."""
    root = Path(workspace).expanduser().resolve(strict=True)
    target = _resolve_target(root, spec.target_path)
    current_text = target.read_text(encoding="utf-8")
    current_sha = _sha256_str(current_text)
    if current_sha != spec.target_source_sha256:
        raise ResultUpdateValidationError("target source changed (stale source hash)")
    try:
        blocks = extract_protected_blocks(current_text)
    except ProtectedBlockViolation as exc:
        raise ResultUpdateValidationError(str(exc)) from exc
    if spec.protected_block_index >= len(blocks):
        raise ResultUpdateValidationError("protected block index out of range")
    old_block = blocks[spec.protected_block_index]
    if _block_hash(old_block) != spec.old_block_sha256:
        raise ResultUpdateValidationError("old block hash mismatch")
    if PROTECTED_START not in proposed_block or PROTECTED_END not in proposed_block:
        raise ResultUpdateValidationError("proposed block must preserve protected markers")
    # Scope: only the indexed block may change.
    candidate = current_text.replace(old_block, proposed_block, 1)
    if candidate.count(PROTECTED_START) != current_text.count(PROTECTED_START):
        raise ResultUpdateValidationError("protected marker count changed")
    # Bound artifact bytes are part of the scientific input (ID alone never binds).
    if spec.allowed_fact_artifact_ids or spec.allowed_table_artifact_ids or spec.allowed_figure_artifact_ids:
        verify_bound_artifact_integrity(root, memory, spec)
        artifact_hashes = _collect_artifact_hashes(root, spec)
    else:
        artifact_hashes = {}
    # Fact/protocol authority: changed/new values need allowlisted bindings.
    facts = list(facts_used or [])
    allowed = set(spec.allowed_fact_ids)
    bindings: list[dict[str, Any]] = []
    protocol_bindings: list[dict[str, Any]] = []
    fact_values = {str(f.get("fact_id")): f for f in facts if isinstance(f, Mapping)}
    protocol_allow = {(str(p.get("name") or "").strip().lower(), str(p.get("unit") or "").strip().lower()): dict(p)
                      for p in spec.allowed_protocol_values}
    old_classified = _classify_candidates(old_block)
    old_texts = {c["text"] for c in old_classified if c["kind"] in {"metric", "protocol"}}
    for cand in _classify_candidates(proposed_block):
        if cand["kind"] == "structural" or cand["text"] in old_texts:
            continue
        if cand["kind"] == "metric":
            matched = None
            for fid, fact in fact_values.items():
                if fid not in allowed:
                    continue
                try:
                    from decimal import Decimal

                    if Decimal(str(cand["number"])) == Decimal(str(fact.get("value"))):
                        matched = fid
                        break
                except Exception:
                    continue
            if matched is None:
                raise ResultUpdateValidationError(
                    f"UNBOUND_SCIENTIFIC_VALUE: {cand['text']!r} has no allowlisted fact binding"
                )
            bindings.append({"occurrence": cand["text"], "fact_id": matched,
                             "canonical": str(fact_values[matched].get("value")),
                             "unit": str(fact_values[matched].get("unit") or cand["unit"])})
        elif cand["kind"] == "protocol":
            key = (re.sub(r"\s+", " ", cand["text"]).strip().lower(), cand["unit"].strip().lower())
            # Match by numeric value + provenance-bearing allowlist entry.
            matched = None
            for (name, unit), entry in protocol_allow.items():
                try:
                    from decimal import Decimal

                    if Decimal(str(cand["number"])) == Decimal(str(entry.get("value"))) and (
                        not unit or unit.lower() == str(entry.get("unit") or "").lower()
                    ):
                        if not str(entry.get("source") or "").strip():
                            continue
                        matched = entry
                        break
                except Exception:
                    continue
            if matched is None:
                raise ResultUpdateValidationError(
                    f"UNBOUND_PROTOCOL_VALUE: {cand['text']!r} needs provenance-bearing protocol binding"
                )
            protocol_bindings.append({"occurrence": cand["text"], "binding": matched})
    # Citations.
    for key in _extract_citations(proposed_block):
        if key not in set(spec.allowed_citation_keys):
            raise ResultUpdateValidationError(f"citation outside allowlist: {key}")
    semantic = _semantic_diff(old_block, proposed_block)
    textual = "".join(difflib.unified_diff(old_block.splitlines(True), proposed_block.splitlines(True)))
    stable = {
        "schema": PROPOSAL_SCHEMA,
        "spec_digest": spec.digest(),
        "target_source_sha256": current_sha,
        "old_block_sha256": spec.old_block_sha256,
        "old_block": old_block,
        "proposed_block": proposed_block,
        "textual_diff": textual,
        "semantic_diff": semantic,
        "facts_used": sorted([str(f.get("fact_id")) for f in facts if isinstance(f, Mapping)]),
        "artifact_ids_used": sorted(artifact_ids_used or []),
        "artifact_hashes": {key: dict(sorted(files.items())) for key, files in artifact_hashes.items()},
        "ledger_bindings": [dict(sorted(d.items())) if isinstance(d, Mapping) else d for d in spec.ledger_bindings],
        "bindings": sorted(bindings, key=lambda b: b["occurrence"]),
        "protocol_bindings": sorted(protocol_bindings, key=lambda b: b["occurrence"]),
        "citations_used": sorted(citations_used or []),
    }
    digest = hashlib.sha256(_canonical(stable)).hexdigest()
    proposal_id = f"prop-{digest[:16]}"
    validation = {
        "spec_integrity": "passed",
        "source_currentness": "passed",
        "block_currentness": "passed",
        "scope_compliance": "passed",
        "fact_bindings": "passed",
        "citation_compliance": "passed",
        "semantic_diff": semantic,
        "overall_status": "needs_human" if semantic else "passed",
    }
    proposal = {**stable, "proposal_id": proposal_id, "proposal_digest": digest,
                "validation": validation, "created_at": utc_now()}
    base = _update_paths(root, proposal_id)
    for name, content in (("spec.json", spec.to_canonical()), ("proposal.json", proposal),
                          ("validation.json", validation)):
        target_path = base / name
        tmp = target_path.with_name(f".{name}.tmp")
        tmp.write_text(json.dumps(content, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(target_path)
    _ensure_update_tables(memory)
    with memory.connect() as db:
        db.execute(
            "INSERT OR REPLACE INTO result_updates (id, spec_digest, proposal_digest, target_path, state, created_at, proposal_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (proposal_id, spec.digest(), digest, spec.target_path,
             "VALIDATED" if validation["overall_status"] == "passed" else "NEEDS_HUMAN",
             str(proposal["created_at"]), json.dumps(proposal, sort_keys=True)),
        )
    return proposal


def approve_proposal(
    workspace: str | Path, memory: Any, proposal_id: str, *,
    approver: str = "local-owner", approval_scope: str = "result_update",
    acknowledged: Sequence[str] | None = None, rationale: str = "",
) -> dict[str, Any]:
    root = Path(workspace).expanduser().resolve(strict=True)
    base = root / ".paperforge" / "result_updates" / proposal_id
    proposal = json.loads((base / "proposal.json").read_text(encoding="utf-8"))
    validation = json.loads((base / "validation.json").read_text(encoding="utf-8"))
    if validation.get("overall_status") == "failed":
        raise ResultUpdateApprovalError("validation failed")
    if validation.get("overall_status") == "needs_human":
        required = [str(d.get("claim")) for d in validation.get("semantic_diff", [])]
        missing = [c for c in required if c not in [str(a) for a in (acknowledged or [])]]
        if missing:
            raise ResultUpdateApprovalError(f"needs_human requires acknowledgement: {missing}")
        if not str(rationale or "").strip():
            raise ResultUpdateApprovalError("needs_human approval requires rationale")
    # Recompute currentness before binding (source, block, artifacts, ledgers).
    spec_data = json.loads((base / "spec.json").read_text(encoding="utf-8"))
    _verify_ledger_bindings(workspace, memory, proposal.get("ledger_bindings") or [])
    _verify_proposal_artifacts(workspace, memory, proposal)
    spec = ResultUpdateSpec(
        target_path=spec_data["target_path"],
        target_source_sha256=proposal["target_source_sha256"],
        protected_block_index=0,
    )
    _ensure_update_tables(memory)
    approved_at = utc_now()
    approval_id = f"appr-{hashlib.sha256(f'{proposal_id}{approved_at}{approver}'.encode()).hexdigest()[:16]}"
    binding = {
        "proposal_id": proposal_id,
        "proposal_digest": proposal["proposal_digest"],
        "target_source_sha256": proposal["target_source_sha256"],
        "old_block_sha256": proposal["old_block_sha256"],
        "artifact_hashes": proposal.get("artifact_hashes") or {},
        "validation_digest": hashlib.sha256(_canonical(validation)).hexdigest(),
        "approval_scope": approval_scope,
        "approver": approver,
        "approved_at": approved_at,
    }
    with memory.connect() as db:
        db.execute(
            "INSERT INTO result_approvals (id, proposal_id, proposal_digest, target_source_sha256,"
            " old_block_sha256, validation_digest, scope, approver, approved_at,"
            " acknowledged_json, rationale, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'valid')",
            (approval_id, proposal_id, binding["proposal_digest"], binding["target_source_sha256"],
             binding["old_block_sha256"], binding["validation_digest"], approval_scope, approver,
             approved_at, json.dumps(sorted(acknowledged or [])), str(rationale or "")),
        )
        db.execute("UPDATE result_updates SET state='APPROVED' WHERE id=?", (proposal_id,))
    (base / "approval.json").write_text(json.dumps({**binding, "acknowledged": sorted(acknowledged or []),
                                                    "rationale": rationale}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"approval_id": approval_id, "binding": binding}


def apply_proposal(workspace: str | Path, memory: Any, proposal_id: str) -> dict[str, Any]:
    root = Path(workspace).expanduser().resolve(strict=True)
    base = root / ".paperforge" / "result_updates" / proposal_id
    proposal = json.loads((base / "proposal.json").read_text(encoding="utf-8"))
    approval_path = base / "approval.json"
    if not approval_path.is_file():
        raise ResultUpdateApprovalError("no approval for proposal")
    approval = json.loads(approval_path.read_text(encoding="utf-8"))
    _ensure_update_tables(memory)
    with memory.connect() as db:
        db.execute("UPDATE result_updates SET state='APPLYING' WHERE id=?", (proposal_id,))
    try:
        spec_data = json.loads((base / "spec.json").read_text(encoding="utf-8"))
        _verify_ledger_bindings(workspace, memory, proposal.get("ledger_bindings") or [])
        _verify_proposal_artifacts(workspace, memory, proposal, expected=approval.get("artifact_hashes"))
        target = _resolve_target(root, spec_data["target_path"])
        current_text = target.read_text(encoding="utf-8")
        if _sha256_str(current_text) != approval["target_source_sha256"]:
            raise ResultUpdateApprovalError("target changed after approval")
        if _sha256_str(current_text) != proposal["target_source_sha256"]:
            raise ResultUpdateApprovalError("proposal stale: source changed")
        blocks = extract_protected_blocks(current_text)
        old_block = proposal["old_block"]
        if old_block not in current_text:
            raise ResultUpdateApprovalError("old block no longer present")
        candidate = current_text.replace(old_block, proposal["proposed_block"], 1)
        # Outside-block bytes must be identical.
        if candidate.replace(proposal["proposed_block"], "") != current_text.replace(old_block, ""):
            raise ResultUpdateApprovalError("scope violation: outside bytes changed")
        tmp = target.with_name(f".{target.name}.{proposal_id}.tmp")
        tmp.write_text(candidate, encoding="utf-8")
        tmp.replace(target)
        new_text = target.read_text(encoding="utf-8")
        new_blocks = extract_protected_blocks(new_text)
        if proposal["proposed_block"] not in new_blocks:
            raise ResultUpdateError("apply verification failed")
        new_source_sha = _sha256_str(new_text)
        new_block_sha = _block_hash(proposal["proposed_block"])
        # Idempotency: same proposal already applied (source equals expected new hash).
        with memory.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO result_baselines (target_path, source_sha256, block_sha256,"
                " proposal_id, approval_id, applied_at) VALUES (?, ?, ?, ?, ?, ?)",
                (spec_data["target_path"], new_source_sha, new_block_sha, proposal_id,
                 approval.get("approval_id", ""), utc_now()),
            )
            db.execute("UPDATE result_updates SET state='APPLIED' WHERE id=?", (proposal_id,))
        (base / "applied.json").write_text(json.dumps({
            "proposal_id": proposal_id, "old_source_sha256": proposal["target_source_sha256"],
            "new_source_sha256": new_source_sha, "old_block_sha256": proposal["old_block_sha256"],
            "new_block_sha256": new_block_sha, "applied_at": utc_now(),
            "result_update_state": "applied", "scientific_review_state": "pending",
            "publication_ready": False,
        }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return {"proposal_id": proposal_id, "new_source_sha256": new_source_sha,
                "new_block_sha256": new_block_sha, "result_update_state": "applied",
                "scientific_review_state": "pending", "publication_ready": False}
    except BaseException as exc:
        with memory.connect() as db:
            try:
                db.execute("UPDATE result_updates SET state='ROLLED_BACK' WHERE id=?", (proposal_id,))
            except OSError:
                pass
        raise


def reconcile_result_updates(workspace: str | Path, memory: Any, *, repair: bool = False) -> dict[str, Any]:
    """Crash-window matrix: CLEAN / RECOVERABLE_FORWARD / ROLLBACK / MANUAL.

    A: APPROVED + target=old -> CLEAN (nothing applied yet).
    B: APPLYING + target=old -> RECOVERABLE_ROLLBACK to APPROVED.
    C: APPLYING + target=new -> RECOVERABLE_FORWARD (complete baseline/applied
       records without touching target again) when bindings still valid.
    D: APPLIED + target=new but applied.json/baseline missing -> FORWARD.
    E/F: target neither old nor new, or bindings invalid -> MANUAL.
    Exception-injection tests cover rollback; true power-loss proof stays U4.
    """
    root = Path(workspace).expanduser().resolve(strict=True)
    _ensure_update_tables(memory)
    findings: list[dict[str, Any]] = []
    with memory.connect() as db:
        rows = db.execute("SELECT * FROM result_updates").fetchall()
        for row in rows:
            proposal_id = str(row["id"])
            state = str(row["state"])
            base = root / ".paperforge" / "result_updates" / proposal_id
            try:
                proposal = json.loads((base / "proposal.json").read_text(encoding="utf-8"))
                spec_data = json.loads((base / "spec.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                findings.append({"proposal_id": proposal_id, "verdict": "MANUAL_INTERVENTION_REQUIRED",
                                 "reason": "proposal files unreadable"})
                continue
            try:
                target = _resolve_target(root, str(spec_data.get("target_path")))
                current_sha = _sha256_str(target.read_text(encoding="utf-8"))
            except (OSError, ResultUpdateError):
                findings.append({"proposal_id": proposal_id, "verdict": "MANUAL_INTERVENTION_REQUIRED",
                                 "reason": "target unreadable"})
                continue
            old_sha, new_expected = str(proposal.get("target_source_sha256") or ""), None
            try:
                old_block, new_block = str(proposal.get("old_block") or ""), str(proposal.get("proposed_block") or "")
                current_text = target.read_text(encoding="utf-8")
                new_expected = _sha256_str(current_text.replace(old_block, new_block, 1)) if old_block in current_text else None
                if old_block not in current_text and new_block in current_text:
                    # Already applied: expected new hash is the current hash.
                    new_expected = current_sha
            except OSError:
                new_expected = None
            applied_path = base / "applied.json"
            with memory.connect() as db2:
                baseline = db2.execute("SELECT * FROM result_baselines WHERE proposal_id=?", (proposal_id,)).fetchone()
            if state in {"APPROVED", "VALIDATED", "NEEDS_HUMAN"} and current_sha == old_sha:
                findings.append({"proposal_id": proposal_id, "verdict": "CLEAN"})
            elif state == "APPLYING" and current_sha == old_sha:
                findings.append({"proposal_id": proposal_id, "verdict": "RECOVERABLE_ROLLBACK"})
                if repair:
                    with memory.connect() as db3:
                        db3.execute("UPDATE result_updates SET state='APPROVED' WHERE id=?", (proposal_id,))
            elif state == "APPLYING" and new_expected is not None and current_sha == new_expected:
                # Forward only when bindings still hold; never rewrite target here.
                try:
                    _verify_ledger_bindings(workspace, memory, proposal.get("ledger_bindings") or [])
                    _verify_proposal_artifacts(workspace, memory, proposal)
                    verdict = "RECOVERABLE_FORWARD"
                except ResultUpdateError as exc:
                    verdict = f"MANUAL_INTERVENTION_REQUIRED: {exc}"
                findings.append({"proposal_id": proposal_id, "verdict": verdict})
                if repair and verdict == "RECOVERABLE_FORWARD":
                    with memory.connect() as db3:
                        db3.execute(
                            "INSERT OR REPLACE INTO result_baselines (target_path, source_sha256, block_sha256,"
                            " proposal_id, approval_id, applied_at) VALUES (?, ?, ?, ?, ?, ?)",
                            (str(spec_data.get("target_path")), current_sha,
                             _block_hash(new_block), proposal_id, "", utc_now()),
                        )
                        db3.execute("UPDATE result_updates SET state='APPLIED' WHERE id=?", (proposal_id,))
                    (base / "applied.json").write_text(json.dumps({
                        "proposal_id": proposal_id, "new_source_sha256": current_sha,
                        "recovered_forward": True, "applied_at": utc_now(),
                        "result_update_state": "applied"}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            elif state == "APPLIED":
                if current_sha != old_sha and applied_path.is_file() and baseline is not None:
                    # Check file wasn't hand-modified after apply.
                    if new_expected is not None and current_sha == new_expected:
                        findings.append({"proposal_id": proposal_id, "verdict": "CLEAN"})
                    else:
                        findings.append({"proposal_id": proposal_id, "verdict": "MANUAL_INTERVENTION_REQUIRED",
                                         "reason": "applied target was hand-modified"})
                elif not applied_path.is_file() or baseline is None:
                    findings.append({"proposal_id": proposal_id, "verdict": "RECOVERABLE_FORWARD",
                                     "reason": "applied record incomplete"})
                    if repair:
                        (base / "applied.json").write_text(json.dumps({
                            "proposal_id": proposal_id, "recovered_forward": True,
                            "applied_at": utc_now()}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                        with memory.connect() as db3:
                            db3.execute("UPDATE result_updates SET state='APPLIED' WHERE id=?", (proposal_id,))
                else:
                    findings.append({"proposal_id": proposal_id, "verdict": "MANUAL_INTERVENTION_REQUIRED",
                                     "reason": "target neither old nor new"})
            else:
                findings.append({"proposal_id": proposal_id, "verdict": "MANUAL_INTERVENTION_REQUIRED",
                                 "reason": f"state={state} with unexpected target hash"})
    verdicts = {f["verdict"].split(":")[0] for f in findings}
    clean = all(v == "CLEAN" for v in verdicts) if findings else True
    return {"findings": findings, "mismatches": [f["proposal_id"] for f in findings if "CLEAN" not in f["verdict"]],
            "consistent": clean}


__all__ = [
    "ProtocolValueBinding",
    "ResultUpdateApprovalError",
    "ResultUpdateError",
    "ResultUpdateSpec",
    "ResultUpdateValidationError",
    "approve_proposal",
    "apply_proposal",
    "build_proposal",
    "reconcile_result_updates",
    "verify_bound_artifact_integrity",
]
