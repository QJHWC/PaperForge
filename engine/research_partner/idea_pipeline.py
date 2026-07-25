from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from .contracts import research_partner_output_skeletons


@dataclass(frozen=True)
class EvidenceContext:
    limitations: tuple[str, ...] = ()
    bottlenecks: tuple[str, ...] = ()
    metrics_summary: tuple[str, ...] = ()
    sources: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class IdeaContext:
    title: str
    description: str
    evidence_basis: tuple[str, ...] = ()
    evidence_context: EvidenceContext = EvidenceContext()


def _normalize_lines(values: tuple[str, ...]) -> list[str]:
    return [value.strip() for value in values if value and value.strip()]


def _problem_statement(
    description: str,
    *,
    limitations: list[str],
    bottlenecks: list[str],
    metrics_summary: list[str],
) -> str:
    segments: list[str] = []
    cleaned = description.strip()
    if cleaned:
        segments.append(cleaned)
    if limitations:
        segments.append(f"已知局限：{limitations[0]}")
    if bottlenecks:
        segments.append(f"已知瓶颈：{bottlenecks[0]}")
    if metrics_summary:
        segments.append(f"现有指标线索：{metrics_summary[0]}")
    if segments:
        return " ".join(segments)
    return "当前提案的问题定义仍偏粗，需要补充更具体的任务背景与约束。"


def _novelty_claim(title: str, evidence_basis: list[str]) -> str:
    if evidence_basis:
        return f"围绕“{title}”整合现有证据线索，提出可验证的结构化研究假设，并显式绑定后续消融实验。"
    return f"围绕“{title}”提出一个待证据补强的研究假设，并先搭建可迭代的 Proposal 骨架。"


def _claim_graph(title: str, evidence_basis: list[str]) -> dict[str, Any]:
    evidence_refs = [f"evidence:{idx}" for idx, _ in enumerate(evidence_basis, start=1)]
    return {
        "nodes": [
            {
                "id": "problem",
                "label": "Problem Framing",
                "type": "problem",
                "summary": "明确现有方法的关键瓶颈与目标任务中的失败模式。",
                "evidence_refs": evidence_refs,
            },
            {
                "id": "method_hypothesis",
                "label": title,
                "type": "method",
                "summary": "构建一个可被 critique 反复修正的核心方法假设。",
                "evidence_refs": evidence_refs,
            },
            {
                "id": "expected_result",
                "label": "Expected Result",
                "type": "expected_result",
                "summary": "通过消融与指标验证方法是否真正缓解目标问题。",
                "evidence_refs": evidence_refs,
            },
        ],
        "edges": [
            {"source": "problem", "target": "method_hypothesis", "relation": "motivates"},
            {"source": "method_hypothesis", "target": "expected_result", "relation": "validated_by"},
        ],
    }


def _experiment_blueprint(title: str, evidence_basis: list[str]) -> dict[str, Any]:
    evidence_note = "优先使用已有文献线索校验假设。" if evidence_basis else "当前缺少直接证据，需要补充文献或实验依据。"
    return {
        "title": title,
        "objective": "验证核心假设是否成立，并确认关键改动是否真正贡献性能提升。",
        "tracks": [
            {
                "id": "core-hypothesis",
                "question": f"{title} 的核心方法假设是否能够针对目标问题形成可观测改进。",
                "success_criteria": [
                    "主指标相较强基线有稳定提升",
                    evidence_note,
                ],
            },
            {
                "id": "ablation-plan",
                "question": "关键模块或设计选择的增益是否可以通过消融实验被解释。",
                "success_criteria": [
                    "至少包含主模块移除或替换实验",
                    "明确记录性能变化与可能原因",
                ],
            },
        ],
        "evidence_basis": evidence_basis,
    }


def run_idea_pipeline(context: IdeaContext) -> dict[str, Any]:
    evidence_basis = _normalize_lines(context.evidence_basis)
    limitations = _normalize_lines(context.evidence_context.limitations)
    bottlenecks = _normalize_lines(context.evidence_context.bottlenecks)
    metrics_summary = _normalize_lines(context.evidence_context.metrics_summary)
    sources = [dict(source) for source in context.evidence_context.sources]
    skeletons = research_partner_output_skeletons()
    idea_brief = {
        **deepcopy(skeletons["idea_brief"]),
        "title": context.title.strip() or "Untitled Proposal",
        "problem": _problem_statement(
            context.description,
            limitations=limitations,
            bottlenecks=bottlenecks,
            metrics_summary=metrics_summary,
        ),
        "novelty_claim": _novelty_claim(context.title.strip() or "Untitled Proposal", evidence_basis),
        "evidence_basis": evidence_basis,
    }
    pipeline_notes = [
        f"evidence_count={len(evidence_basis)}",
        f"limitations={len(limitations)}",
        f"bottlenecks={len(bottlenecks)}",
        f"metrics={len(metrics_summary)}",
        f"sources={len(sources)}",
        "assembled deterministic idea package",
    ]
    if not context.description.strip():
        pipeline_notes.append("description_gap_detected")

    return {
        "idea_brief": idea_brief,
        "claim_graph": _claim_graph(idea_brief["title"], evidence_basis),
        "experiment_blueprint": _experiment_blueprint(idea_brief["title"], evidence_basis),
        "pipeline_notes": pipeline_notes,
        "revision_history": [],
        "evidence_context": {
            "limitations": limitations,
            "bottlenecks": bottlenecks,
            "metrics_summary": metrics_summary,
            "sources": sources,
        },
    }


def revise_idea_package(
    idea_package: Mapping[str, Any],
    critique_report: Mapping[str, Any],
    round_index: int,
) -> dict[str, Any]:
    revised_package = deepcopy(dict(idea_package))
    revised_idea_brief = deepcopy(dict(revised_package["idea_brief"]))
    revised_blueprint = deepcopy(dict(revised_package["experiment_blueprint"]))
    revised_notes = list(revised_package.get("pipeline_notes", []))
    revised_history = list(revised_package.get("revision_history", []))

    revised_idea_brief["novelty_claim"] = (
        revised_idea_brief["novelty_claim"].rstrip("。")
        + f"；已根据第 {round_index} 轮 critique 收紧论断边界。"
    )

    tracks = list(revised_blueprint.get("tracks", []))
    track_ids = {track.get("id") for track in tracks}
    if "evidence-gap-review" not in track_ids:
        tracks.append(
            {
                "id": "evidence-gap-review",
                "question": "当前证据缺口是否会影响结论可信度。",
                "success_criteria": [
                    "明确列出仍需补充的文献或实验",
                    "避免在证据不足时做过强结论",
                ],
            }
        )
    revised_blueprint["tracks"] = tracks

    revised_notes.append(f"round_{round_index}_revision_applied")
    revised_history.append(
        {
            "round": round_index,
            "summary": critique_report.get("summary", ""),
            "risks": list(critique_report.get("risks", [])),
        }
    )

    return {
        **revised_package,
        "idea_brief": revised_idea_brief,
        "experiment_blueprint": revised_blueprint,
        "pipeline_notes": revised_notes,
        "revision_history": revised_history,
    }
