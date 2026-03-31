from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

RESEARCH_PARTNER_CONTRACT_VERSION = "v1"
CORE_OUTPUT_FILES = {
    "idea_brief": "idea_brief.json",
    "claim_graph": "claim_graph.json",
    "critique_report": "critique_report.json",
}
SUPPLEMENTAL_OUTPUT_FILES = {
    "proposal_brief": "proposal_brief.md",
    "experiment_blueprint": "experiment_blueprint.json",
    "expected_figures": "expected_figures.json",
    "rubric_scorecard": "rubric_scorecard.json",
    "evidence_index": "evidence_index.json",
    "manifest": "manifest.json",
}
RESEARCH_PARTNER_PLANNED_OUTPUT_KEYS = (
    *CORE_OUTPUT_FILES,
    *SUPPLEMENTAL_OUTPUT_FILES,
)


def research_partner_artifacts_dir(workspace: Path | None) -> Path | None:
    if workspace is None:
        return None
    return workspace / "artifacts" / "research_partner"


def research_partner_output_contracts() -> Dict[str, Any]:
    return {
        "required": ["idea_brief", "claim_graph", "critique_report"],
        "properties": {
            "idea_brief": {
                "type": "object",
                "required": ["title", "problem", "novelty_claim", "evidence_basis"],
                "properties": {
                    "title": {"type": "string"},
                    "problem": {"type": "string"},
                    "novelty_claim": {"type": "string"},
                    "evidence_basis": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
            "claim_graph": {
                "type": "object",
                "required": ["nodes", "edges"],
                "properties": {
                    "nodes": {"type": "array", "items": {"type": "object"}},
                    "edges": {"type": "array", "items": {"type": "object"}},
                },
            },
            "critique_report": {
                "type": "object",
                "required": ["summary", "strengths", "risks", "open_questions"],
                "properties": {
                    "summary": {"type": "string"},
                    "strengths": {"type": "array", "items": {"type": "string"}},
                    "risks": {"type": "array", "items": {"type": "string"}},
                    "open_questions": {"type": "array", "items": {"type": "string"}},
                },
            },
            "proposal_brief": {
                "type": "string",
            },
            "experiment_blueprint": {
                "type": "object",
                "required": ["title", "tracks"],
                "properties": {
                    "title": {"type": "string"},
                    "tracks": {"type": "array", "items": {"type": "object"}},
                },
            },
            "expected_figures": {
                "type": "object",
                "required": ["figures"],
                "properties": {
                    "figures": {"type": "array", "items": {"type": "object"}},
                },
            },
            "rubric_scorecard": {
                "type": "object",
                "required": ["overall", "threshold", "max_rounds", "rubric_name", "dimension_scores", "review_mode", "blocking_issues", "recommendation", "committee_decision"],
                "properties": {
                    "overall": {"type": "number"},
                    "threshold": {"type": "number"},
                    "max_rounds": {"type": "integer"},
                    "rubric_name": {"type": "string"},
                    "dimension_scores": {"type": "object"},
                    "review_mode": {"type": "string"},
                    "blocking_issues": {"type": "array", "items": {"type": "string"}},
                    "recommendation": {"type": "string"},
                    "committee_decision": {"type": ["object", "null"]},
                },
            },
            "evidence_index": {
                "type": "object",
                "required": ["sources"],
                "properties": {
                    "sources": {"type": "array", "items": {"type": "object"}},
                },
            },
            "manifest": {
                "type": "object",
                "required": ["contract_version", "generated_at", "workspace", "generated_files", "critique_decision", "scorecard"],
                "properties": {
                    "contract_version": {"type": "string"},
                    "generated_at": {"type": "string"},
                    "workspace": {"type": "string"},
                    "generated_files": {"type": "object"},
                    "critique_decision": {"type": "string"},
                    "scorecard": {"type": "object"},
                },
            },
        },
    }


def research_partner_output_skeletons() -> Dict[str, Any]:
    rubric_scorecard = {
        "overall": 0.0,
        "threshold": 0.0,
        "max_rounds": 0,
        "rubric_name": "",
        "dimension_scores": {},
        "review_mode": "",
        "blocking_issues": [],
        "recommendation": "revise",
        "committee_decision": None,
    }
    return {
        "idea_brief": {
            "title": "",
            "problem": "",
            "novelty_claim": "",
            "evidence_basis": [],
        },
        "claim_graph": {"nodes": [], "edges": []},
        "critique_report": {
            "summary": "",
            "strengths": [],
            "risks": [],
            "open_questions": [],
        },
        "proposal_brief": "",
        "experiment_blueprint": {
            "title": "",
            "tracks": [],
        },
        "expected_figures": {
            "figures": [],
        },
        "rubric_scorecard": rubric_scorecard,
        "evidence_index": {
            "sources": [],
        },
        "manifest": {
            "contract_version": RESEARCH_PARTNER_CONTRACT_VERSION,
            "generated_at": "",
            "workspace": ".",
            "source_files": {},
            "rubric_profile": "",
            "evidence_files": [],
            "generated_files": {},
            "workflow_state": {},
            "pipeline_notes": [],
            "revision_history": [],
            "critique_decision": "needs_revision",
            "critique_rounds": [],
            "scorecard": dict(rubric_scorecard),
        },
    }


def research_partner_planned_outputs(
    workspace: Path | None,
    *,
    materialized: bool = False,
) -> Dict[str, Dict[str, Any]]:
    skeletons = research_partner_output_skeletons()
    status = "completed" if materialized else "planned"
    output_files = {**CORE_OUTPUT_FILES, **SUPPLEMENTAL_OUTPUT_FILES}
    formats = {
        "proposal_brief": "markdown",
    }
    return {
        output_name: {
            "format": formats.get(output_name, "json"),
            "status": status,
            "path": (Path("artifacts") / "research_partner" / filename).as_posix() if workspace else None,
            "skeleton": skeletons[output_name],
        }
        for output_name, filename in output_files.items()
    }


def research_partner_artifact_paths(workspace: Path | None) -> list[Path | None]:
    artifacts_dir = research_partner_artifacts_dir(workspace)
    if artifacts_dir is None:
        return []
    return [
        artifacts_dir / filename
        for filename in [
            *CORE_OUTPUT_FILES.values(),
            *SUPPLEMENTAL_OUTPUT_FILES.values(),
        ]
    ]
