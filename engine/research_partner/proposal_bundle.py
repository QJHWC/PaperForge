from __future__ import annotations

import csv
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from engine.mvp_workflow import load_idea_metadata, load_workflow_state
from engine.perform_review import load_paper

from .contracts import (
    CORE_OUTPUT_FILES,
    RESEARCH_PARTNER_CONTRACT_VERSION,
    SUPPLEMENTAL_OUTPUT_FILES,
    research_partner_artifacts_dir,
    research_partner_output_skeletons,
)
from .critique_loop import CritiqueLoopConfig, run_critique_loop
from .idea_pipeline import EvidenceContext, IdeaContext, run_idea_pipeline
from .rubric_loader import load_rubric_profile


def _read_notes(workspace: Path) -> str:
    notes_path = workspace / "notes.txt"
    if not notes_path.exists():
        return ""
    return notes_path.read_text(encoding="utf-8").strip()


def _extract_literature_items(notes_text: str) -> list[str]:
    items: list[str] = []
    in_block = False
    for raw_line in notes_text.splitlines():
        line = raw_line.strip()
        if line == "## Literature Snapshot":
            in_block = True
            continue
        if in_block and line.startswith("## "):
            break
        if not in_block:
            continue
        if re.match(r"^\d+\.\s+", line):
            items.append(re.sub(r"^\d+\.\s+", "", line))
    return items


def _first_non_empty(*values: str) -> str:
    for value in values:
        cleaned = value.strip()
        if cleaned:
            return cleaned
    return ""


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _build_proposal_brief(
    idea_brief: Dict[str, Any],
    critique_report: Dict[str, Any],
    experiment_blueprint: Dict[str, Any],
) -> str:
    strength_lines = "\n".join(f"- {item}" for item in critique_report["strengths"])
    risk_lines = "\n".join(f"- {item}" for item in critique_report["risks"])
    question_lines = "\n".join(f"- {item}" for item in critique_report["open_questions"])
    evidence_lines = "\n".join(f"- {item}" for item in idea_brief["evidence_basis"])
    track_lines = "\n".join(
        f"- {track['id']}: {track['question']}" for track in experiment_blueprint["tracks"]
    )
    return "\n".join(
        [
            f"# {idea_brief['title']}",
            "",
            "## Problem",
            idea_brief["problem"],
            "",
            "## Novelty Claim",
            idea_brief["novelty_claim"],
            "",
            "## Evidence Basis",
            evidence_lines or "- 暂无证据条目",
            "",
            "## Strengths",
            strength_lines,
            "",
            "## Risks",
            risk_lines,
            "",
            "## Open Questions",
            question_lines,
            "",
            "## Experiment Tracks",
            track_lines,
        ]
    ).strip() + "\n"


def _artifact_safe_path(path_value: str | Path, workspace: Path) -> str:
    candidate = Path(path_value).expanduser()
    resolved = candidate.resolve() if candidate.is_absolute() else (workspace / candidate).resolve()
    try:
        return resolved.relative_to(workspace).as_posix()
    except ValueError:
        name = resolved.name.strip()
        return name or "[redacted]"


def _resolve_workspace_evidence_file(workspace: Path, raw_path: str) -> tuple[Path, str]:
    candidate = Path(raw_path).expanduser()
    resolved = candidate.resolve() if candidate.is_absolute() else (workspace / candidate).resolve()
    try:
        relative = resolved.relative_to(workspace)
    except ValueError as exc:
        raise ValueError(f"evidence file must stay within workspace: {raw_path}") from exc
    if not resolved.exists():
        raise ValueError(f"evidence file not found within workspace: {raw_path}")
    if not resolved.is_file():
        raise ValueError(f"evidence file must be a regular file within workspace: {raw_path}")
    return resolved, relative.as_posix()


def _normalize_evidence_inputs(workspace: Path, evidence_files: list[str] | None) -> tuple[list[str], list[str]]:
    resolved_paths: list[str] = []
    relative_paths: list[str] = []
    for item in evidence_files or []:
        raw_path = str(item).strip()
        if not raw_path:
            continue
        resolved, relative = _resolve_workspace_evidence_file(workspace, raw_path)
        resolved_paths.append(str(resolved))
        relative_paths.append(relative)
    return resolved_paths, relative_paths


def _sanitize_source_records(workspace: Path, sources: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
    sanitized_sources: list[Dict[str, Any]] = []
    for source in sources:
        record = dict(source)
        path_value = record.get("path")
        if path_value:
            record["path"] = _artifact_safe_path(str(path_value), workspace)
        sanitized_sources.append(record)
    return sanitized_sources


def _sanitize_evidence_index(workspace: Path, evidence_index: Dict[str, Any]) -> Dict[str, Any]:
    sanitized = dict(evidence_index)
    sanitized["sources"] = _sanitize_source_records(workspace, list(evidence_index.get("sources", [])))
    return sanitized


def _sanitize_path_mapping(workspace: Path, mapping: Dict[str, str]) -> Dict[str, str]:
    return {key: _artifact_safe_path(path_value, workspace) for key, path_value in mapping.items()}


def _normalize_source_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return "pdf"
    if suffix == ".csv":
        return "csv"
    return suffix.lstrip(".") or "file"


def _summarize_pdf(path: Path) -> str:
    try:
        text = load_paper(str(path), num_pages=2, min_size=20)
    except Exception:
        return f"PDF evidence imported from {path.name}."
    compact = " ".join(segment.strip() for segment in text.splitlines() if segment.strip())
    if not compact:
        return f"PDF evidence imported from {path.name}."
    return compact[:240]


def _summarize_csv(path: Path) -> tuple[str, list[str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        rows = [[cell.strip() for cell in row] for row in csv.reader(handle) if any(cell.strip() for cell in row)]
    if len(rows) <= 1:
        return (f"CSV evidence imported from {path.name}.", [])
    header = rows[0]
    records = rows[1:]
    if len(header) < 2 or not records:
        return (f"CSV evidence imported from {path.name}.", [])
    metric_names = header[1:]
    metrics_summary: list[str] = []
    first_record = records[0]
    if len(records) >= 2:
        baseline = records[0]
        candidate = records[1]
        candidate_name = candidate[0] if candidate else "candidate"
        for idx, metric_name in enumerate(metric_names, start=1):
            if idx >= len(baseline) or idx >= len(candidate):
                continue
            metrics_summary.append(f"{candidate_name} {metric_name} {candidate[idx]} (baseline {baseline[idx]})")
    else:
        row_name = first_record[0] if first_record else "row1"
        for idx, metric_name in enumerate(metric_names, start=1):
            if idx >= len(first_record):
                continue
            metrics_summary.append(f"{row_name} {metric_name} {first_record[idx]}")
    summary = metrics_summary[0] if metrics_summary else f"CSV evidence imported from {path.name}."
    return summary, metrics_summary


def ingest_evidence_context(evidence_files: list[str] | None) -> tuple[tuple[str, ...], Dict[str, Any], Dict[str, Any]]:
    files = [Path(item).expanduser().resolve() for item in (evidence_files or []) if str(item).strip()]
    evidence_basis: list[str] = []
    limitations: list[str] = []
    bottlenecks: list[str] = []
    metrics_summary: list[str] = []
    sources: list[Dict[str, Any]] = []

    for file_path in files:
        kind = _normalize_source_kind(file_path)
        if kind == "pdf":
            summary = _summarize_pdf(file_path)
            evidence_basis.append(summary)
            limitations.append(summary)
            bottlenecks.append(summary)
            sources.append({"kind": kind, "path": str(file_path), "summary": summary})
            continue
        if kind == "csv":
            summary, csv_metrics = _summarize_csv(file_path)
            evidence_basis.append(summary)
            metrics_summary.extend(csv_metrics)
            sources.append({"kind": kind, "path": str(file_path), "summary": summary})
            continue
        summary = f"External evidence imported from {file_path.name}."
        evidence_basis.append(summary)
        sources.append({"kind": kind, "path": str(file_path), "summary": summary})

    evidence_context = {
        "limitations": limitations,
        "bottlenecks": bottlenecks,
        "metrics_summary": metrics_summary,
        "sources": sources,
    }
    evidence_index = {
        "sources": [{"kind": item["kind"], "path": item["path"]} for item in sources],
    }
    return tuple(evidence_basis), evidence_context, evidence_index


def _build_expected_figures(
    idea_brief: Dict[str, Any],
    experiment_blueprint: Dict[str, Any],
    critique_report: Dict[str, Any],
) -> Dict[str, Any]:
    figures: list[Dict[str, Any]] = []
    for index, track in enumerate(experiment_blueprint.get("tracks", []), start=1):
        track_id = str(track.get("id") or f"track_{index}")
        figures.append(
            {
                "id": f"fig_{index}",
                "title": f"{track.get('goal', track.get('question', 'Experiment result'))}",
                "kind": "chart",
                "source_track": track_id,
                "purpose": str(track.get("question") or idea_brief.get("novelty_claim") or "验证核心假设。"),
                "success_signal": str(track.get("success_signal") or "结果趋势支持核心假设。"),
            }
        )
    if not figures:
        figures.append(
            {
                "id": "fig_1",
                "title": "Core hypothesis verification",
                "kind": "chart",
                "source_track": None,
                "purpose": idea_brief.get("novelty_claim", "验证核心主张。"),
                "success_signal": critique_report.get("summary", "输出首轮 proposal 验证图。"),
            }
        )
    return {"figures": figures}


def _build_rubric_scorecard(critique_result: Dict[str, Any]) -> Dict[str, Any]:
    scorecard = dict(research_partner_output_skeletons()["rubric_scorecard"])
    scorecard.update(dict(critique_result.get("scorecard", {})))
    scorecard["committee_decision"] = critique_result.get("committee_decision")
    return scorecard


def materialize_proposal_bundle(
    *,
    workspace: Path,
    title: str,
    description: str,
    rubric_profile: str = "default",
    evidence_files: list[str] | None = None,
) -> Dict[str, Any]:
    workspace = workspace.expanduser().resolve()
    artifacts_dir = research_partner_artifacts_dir(workspace)
    if artifacts_dir is None:
        raise ValueError("workspace is required")
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    idea_metadata = load_idea_metadata(str(workspace))
    workflow_state = load_workflow_state(str(workspace))
    notes_text = _read_notes(workspace)
    evidence_basis = _extract_literature_items(notes_text)
    evidence_context = EvidenceContext()
    evidence_index: Dict[str, Any] = {"sources": []}
    normalized_evidence_files: list[str] = []
    resolved_evidence_files, normalized_evidence_files = _normalize_evidence_inputs(workspace, evidence_files)
    if resolved_evidence_files:
        ingested_basis, ingested_context, evidence_index = ingest_evidence_context(resolved_evidence_files)
        evidence_basis = list(ingested_basis)
        sanitized_sources = _sanitize_source_records(workspace, list(ingested_context.get("sources", [])))
        evidence_context = EvidenceContext(
            limitations=tuple(ingested_context.get("limitations", [])),
            bottlenecks=tuple(ingested_context.get("bottlenecks", [])),
            metrics_summary=tuple(ingested_context.get("metrics_summary", [])),
            sources=tuple(sanitized_sources),
        )
        evidence_index = _sanitize_evidence_index(workspace, evidence_index)
    if not evidence_basis:
        evidence_basis = ["Bootstrap notes 尚未包含文献清单，当前 proposal 依据标题与描述生成。"]

    resolved_title = _first_non_empty(str(idea_metadata.get("Title") or ""), title, "Untitled Proposal")
    resolved_description = _first_non_empty(
        str(idea_metadata.get("Experiment") or ""),
        description,
        "",
    )

    idea_package = run_idea_pipeline(
        IdeaContext(
            title=resolved_title,
            description=resolved_description,
            evidence_basis=tuple(evidence_basis),
            evidence_context=evidence_context,
        )
    )
    critique_result = run_critique_loop(
        idea_package,
        CritiqueLoopConfig(
            rubric_profile=load_rubric_profile(rubric_profile),
            review_mode="llm_committee",
            review_model="gpt-5.4-xhigh",
            reviewer_count=3,
        ),
    )
    final_idea_package = critique_result["idea_package"]
    idea_brief = final_idea_package["idea_brief"]
    claim_graph = final_idea_package["claim_graph"]
    experiment_blueprint = final_idea_package["experiment_blueprint"]
    critique_report = critique_result["critique_report"]
    proposal_brief = _build_proposal_brief(idea_brief, critique_report, experiment_blueprint)
    expected_figures = _build_expected_figures(idea_brief, experiment_blueprint, critique_report)
    rubric_scorecard = _build_rubric_scorecard(critique_result)

    _write_json(artifacts_dir / CORE_OUTPUT_FILES["idea_brief"], idea_brief)
    _write_json(artifacts_dir / CORE_OUTPUT_FILES["claim_graph"], claim_graph)
    _write_json(artifacts_dir / CORE_OUTPUT_FILES["critique_report"], critique_report)
    _write_json(artifacts_dir / SUPPLEMENTAL_OUTPUT_FILES["experiment_blueprint"], experiment_blueprint)
    _write_json(artifacts_dir / SUPPLEMENTAL_OUTPUT_FILES["expected_figures"], expected_figures)
    _write_json(artifacts_dir / SUPPLEMENTAL_OUTPUT_FILES["rubric_scorecard"], rubric_scorecard)
    _write_json(artifacts_dir / SUPPLEMENTAL_OUTPUT_FILES["evidence_index"], evidence_index)
    (artifacts_dir / SUPPLEMENTAL_OUTPUT_FILES["proposal_brief"]).write_text(proposal_brief, encoding="utf-8")

    manifest = {
        "contract_version": RESEARCH_PARTNER_CONTRACT_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "workspace": ".",
        "source_files": _sanitize_path_mapping(
            workspace,
            {
                "workflow_idea": str(workspace / "workflow_idea.json"),
                "workflow_state": str(workspace / "workflow_state.json"),
                "notes": str(workspace / "notes.txt"),
            },
        ),
        "rubric_profile": rubric_profile,
        "evidence_files": normalized_evidence_files,
        "generated_files": _sanitize_path_mapping(
            workspace,
            {
                "idea_brief": str(artifacts_dir / CORE_OUTPUT_FILES["idea_brief"]),
                "claim_graph": str(artifacts_dir / CORE_OUTPUT_FILES["claim_graph"]),
                "critique_report": str(artifacts_dir / CORE_OUTPUT_FILES["critique_report"]),
                "proposal_brief": str(artifacts_dir / SUPPLEMENTAL_OUTPUT_FILES["proposal_brief"]),
                "experiment_blueprint": str(artifacts_dir / SUPPLEMENTAL_OUTPUT_FILES["experiment_blueprint"]),
                "expected_figures": str(artifacts_dir / SUPPLEMENTAL_OUTPUT_FILES["expected_figures"]),
                "rubric_scorecard": str(artifacts_dir / SUPPLEMENTAL_OUTPUT_FILES["rubric_scorecard"]),
                "evidence_index": str(artifacts_dir / SUPPLEMENTAL_OUTPUT_FILES["evidence_index"]),
            },
        ),
        "workflow_state": workflow_state,
        "pipeline_notes": list(final_idea_package.get("pipeline_notes", [])),
        "revision_history": list(final_idea_package.get("revision_history", [])),
        "critique_decision": critique_result["decision"],
        "critique_rounds": critique_result["rounds"],
        "scorecard": rubric_scorecard,
    }
    _write_json(artifacts_dir / SUPPLEMENTAL_OUTPUT_FILES["manifest"], manifest)

    return {
        "idea_brief": idea_brief,
        "claim_graph": claim_graph,
        "critique_report": critique_report,
        "experiment_blueprint": experiment_blueprint,
        "proposal_brief_path": str(artifacts_dir / SUPPLEMENTAL_OUTPUT_FILES["proposal_brief"]),
        "manifest_path": str(artifacts_dir / SUPPLEMENTAL_OUTPUT_FILES["manifest"]),
        "pipeline_notes": list(final_idea_package.get("pipeline_notes", [])),
        "revision_history": list(final_idea_package.get("revision_history", [])),
        "critique_decision": critique_result["decision"],
        "scorecard": rubric_scorecard,
    }
