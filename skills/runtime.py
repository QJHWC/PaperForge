from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SKILLS_ROOT = ROOT / "skills"


def load_skill_schema(skill_id: str) -> dict[str, Any]:
    path = SKILLS_ROOT / skill_id / "schema.json"
    return json.loads(path.read_text(encoding="utf-8"))


def load_example(skill_id: str, filename: str) -> dict[str, Any]:
    path = SKILLS_ROOT / skill_id / filename
    return json.loads(path.read_text(encoding="utf-8"))


def available_skill_ids() -> list[str]:
    skill_ids: list[str] = []
    for path in sorted(SKILLS_ROOT.iterdir()):
        if not path.is_dir():
            continue
        if (path / "schema.json").exists():
            skill_ids.append(path.name)
    return skill_ids


def _require(payload: dict[str, Any], *keys: str) -> None:
    missing = [key for key in keys if key not in payload]
    if missing:
        raise ValueError(f"Missing required keys: {', '.join(missing)}")


def _summarize_text(text: str, limit: int = 240) -> str:
    normalized = re.sub(r"\s+", " ", text).strip()
    return normalized[:limit]


def _run_write_section(payload: dict[str, Any]) -> dict[str, Any]:
    _require(payload, "section_name", "notes")
    section_name = str(payload["section_name"]).strip()
    notes = _summarize_text(str(payload.get("notes", "")))
    style = _summarize_text(str(payload.get("style_policy", "clear, concise, paper-ready")))
    existing = _summarize_text(str(payload.get("existing_tex_context", "")))
    draft = (
        f"\\section{{{section_name}}}\n"
        f"{notes or 'Draft content pending supporting notes.'}\n\n"
        f"% Style policy: {style}\n"
    )
    if existing:
        draft += f"% Existing context: {existing}\n"
    return {
        "section_name": section_name,
        "draft_text": draft,
        "outline": [
            f"State the purpose of {section_name}.",
            "Ground the section in available notes and experiment evidence.",
            "Keep the narrative compatible with downstream citation/refinement phases.",
        ],
    }


def _run_refine_section(payload: dict[str, Any]) -> dict[str, Any]:
    _require(payload, "section_name", "section_text")
    text = re.sub(r"\s+", " ", str(payload["section_text"])).strip()
    tips = str(payload.get("tips", "")).strip()
    refined = text
    if tips:
        refined = f"{refined}\n\n[Refinement focus] {tips}"
    return {
        "section_name": str(payload["section_name"]).strip(),
        "refined_text": refined,
        "change_summary": "Normalized whitespace and attached explicit refinement focus.",
    }


def _run_citation_gap(payload: dict[str, Any]) -> dict[str, Any]:
    _require(payload, "draft")
    draft = _summarize_text(str(payload["draft"]), limit=400)
    if not draft:
        return {
            "Description": "Draft is empty; establish the core related-work claim first.",
            "Query": "recent survey related work",
        }
    first_sentence = re.split(r"(?<=[.!?])\s+", draft)[0]
    keywords = " ".join(first_sentence.split()[:12]).strip()
    return {
        "Description": f"补齐与以下陈述最相关的引用: {first_sentence}",
        "Query": keywords or "related work query",
    }


def _run_citation_select(payload: dict[str, Any]) -> dict[str, Any]:
    _require(payload, "papers")
    papers = payload.get("papers", [])
    if not isinstance(papers, list):
        raise ValueError("papers must be a list")
    selected = papers[: min(3, len(papers))]
    titles = [str(paper.get("title") or paper.get("Title") or "") for paper in selected if isinstance(paper, dict)]
    return {
        "Selected": selected,
        "Description": "优先保留标题最完整的候选文献。" if titles else "没有可选文献。",
        "selected_titles": [title for title in titles if title],
    }


def _run_latex_fix(payload: dict[str, Any]) -> dict[str, Any]:
    _require(payload, "latex_text")
    text = str(payload["latex_text"])
    fixed = text.replace("\t", "    ")
    return {
        "latex_text": fixed,
        "fixes_applied": [
            "normalized tabs to spaces",
        ],
        "diagnostics": payload.get("diagnostics", []),
    }


def _run_de_aigc_rewrite(payload: dict[str, Any]) -> dict[str, Any]:
    _require(payload, "text")
    text = re.sub(r"\s+", " ", str(payload["text"])).strip()
    rewrites = text.replace("In this paper, it can be seen that", "This paper shows that")
    rewrites = rewrites.replace("In this paper", "This paper")
    rewrites = rewrites.replace("it can be seen that", "we observe that")
    rewrites = re.sub(r"\s{2,}", " ", rewrites).strip()
    return {
        "rewritten_text": rewrites,
        "style_notes": "Collapsed filler phrasing and normalized spacing.",
    }


SKILL_EXECUTORS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "write-section": _run_write_section,
    "refine-section": _run_refine_section,
    "citation-gap": _run_citation_gap,
    "citation-select": _run_citation_select,
    "latex-fix": _run_latex_fix,
    "de-aigc-rewrite": _run_de_aigc_rewrite,
}


def run_skill(skill_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    if skill_id not in SKILL_EXECUTORS:
        raise ValueError(f"Unsupported skill: {skill_id}")
    schema = load_skill_schema(skill_id)
    result = SKILL_EXECUTORS[skill_id](payload)
    return {
        "skill": skill_id,
        "schema": schema,
        "input": payload,
        "output": result,
    }
