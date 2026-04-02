from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Dict, Mapping

from engine.llm import create_client
from engine.perform_review import perform_review

from .idea_pipeline import revise_idea_package
from .rubric_loader import RubricProfile, load_rubric_profile


@dataclass(frozen=True)
class CritiqueLoopConfig:
    rubric_profile: RubricProfile | None = None
    rubric_name: str = "default"
    review_mode: str = "deterministic"
    review_model: str = "gpt-5.4-xhigh"
    reviewer_count: int = 1


def _clamp_score(value: float) -> float:
    return max(0.0, min(round(value, 2), 1.0))


def _score_dimension(name: str, *, description_present: bool, evidence_count: int, track_count: int) -> float:
    if name == "novelty":
        return 0.85 if description_present else 0.35
    if name == "methodology":
        if track_count >= 2:
            return 0.8
        if track_count == 1:
            return 0.55
        return 0.3
    if name == "evidence_support":
        if evidence_count >= 3:
            return 0.9
        if evidence_count >= 1:
            return 0.55
        return 0.2
    if name == "clarity":
        return 0.75 if description_present else 0.4
    if name == "feasibility":
        return 0.7 if track_count >= 2 else 0.45
    return 0.5


def _build_round_report(
    idea_package: Mapping[str, Any],
    round_index: int,
    profile: RubricProfile,
) -> Dict[str, Any]:
    idea_brief = idea_package["idea_brief"]
    evidence_basis = list(idea_brief.get("evidence_basis", []))
    evidence_count = len(evidence_basis)
    description = str(idea_brief.get("problem") or "").strip()
    blueprint = idea_package.get("experiment_blueprint", {})
    tracks = list(blueprint.get("tracks", []))
    description_present = bool(description)

    strengths = []
    risks = []
    open_questions = []
    dimension_scores: dict[str, float] = {}

    for dimension in profile.dimensions:
        dimension_scores[dimension.name] = _score_dimension(
            dimension.name,
            description_present=description_present,
            evidence_count=evidence_count,
            track_count=len(tracks),
        )

    score = sum(dimension_scores[item.name] * item.weight for item in profile.dimensions)
    score = _clamp_score(score)

    if description_present:
        strengths.append("问题定义已显式写出，便于 reviewer 判断任务边界。")
    else:
        risks.append("问题定义仍然过于模糊，难以判断主张是否成立。")

    if evidence_count >= 3:
        strengths.append(f"已提供 {evidence_count} 条证据线索，可支撑首轮 proposal 论证。")
    elif evidence_count >= 1:
        strengths.append(f"已有 {evidence_count} 条证据线索，但仍需补强证据覆盖。")
        risks.append("证据数量偏少，关键 claim 的支撑仍不充分。")
        open_questions.append("是否还存在能直接支持核心假设的高质量参考工作。")
    else:
        risks.append("当前没有可回链的证据条目，proposal 可信度不足。")
        open_questions.append("需要补充哪些文献、实验或先验知识来支撑核心假设。")

    if len(tracks) >= 2:
        strengths.append("实验蓝图至少包含主假设验证与消融计划。")
    else:
        risks.append("实验蓝图过于单薄，尚不足以验证关键设计。")

    summary = (
        f"第 {round_index} 轮 critique 完成：当前 proposal 综合评分为 {score:.2f}，"
        f"使用 rubric={profile.name}。"
    )
    if not open_questions:
        open_questions.append("下一轮是否需要切换到更严格的 venue/rubric profile。")

    return {
        "round": round_index,
        "overall": score,
        "summary": summary,
        "strengths": strengths,
        "risks": risks,
        "open_questions": open_questions,
        "dimension_scores": dimension_scores,
        "review_mode": "fallback_deterministic",
    }


def _dimension_score_from_review(name: str, review: Mapping[str, Any]) -> float:
    if name == "novelty":
        return _clamp_score(float(review.get("Originality", 0)) / 4.0)
    if name == "methodology":
        quality = float(review.get("Quality", 0)) / 4.0
        soundness = float(review.get("Soundness", 0)) / 4.0
        contribution = float(review.get("Contribution", 0)) / 4.0
        return _clamp_score((quality + soundness + contribution) / 3.0)
    if name == "evidence_support":
        quality = float(review.get("Quality", 0)) / 4.0
        soundness = float(review.get("Soundness", 0)) / 4.0
        return _clamp_score((quality + soundness) / 2.0)
    if name == "clarity":
        clarity = float(review.get("Clarity", 0)) / 4.0
        presentation = float(review.get("Presentation", 0)) / 4.0
        return _clamp_score((clarity + presentation) / 2.0)
    if name == "feasibility":
        soundness = float(review.get("Soundness", 0)) / 4.0
        significance = float(review.get("Significance", 0)) / 4.0
        return _clamp_score((soundness + significance) / 2.0)
    return _clamp_score(float(review.get("Overall", 0)) / 10.0)


def _collect_blocking_issues(report: Mapping[str, Any], threshold: float) -> list[str]:
    issues: list[str] = []
    overall = float(report.get("overall", 0.0))
    if overall < threshold:
        issues.append(f"overall_below_threshold:{overall:.2f}<{threshold:.2f}")
    for risk in report.get("risks", []):
        text = str(risk).strip()
        if text:
            issues.append(text)
    return issues


def _recommendation_for_report(report: Mapping[str, Any], threshold: float) -> str:
    blocking_issues = _collect_blocking_issues(report, threshold)
    if blocking_issues:
        return "revise"
    return "go"


def _decision_from_report(report: Mapping[str, Any], threshold: float) -> str:
    recommendation = _recommendation_for_report(report, threshold)
    if recommendation == "go":
        return "approved"
    return "needs_revision"


def _build_review_text(idea_package: Mapping[str, Any]) -> str:
    idea_brief = idea_package.get("idea_brief", {})
    claim_graph = idea_package.get("claim_graph", {})
    experiment_blueprint = idea_package.get("experiment_blueprint", {})
    evidence_basis = "\n".join(f"- {item}" for item in idea_brief.get("evidence_basis", [])) or "- 无"
    tracks = "\n".join(
        f"- {track.get('id', 'track')}: {track.get('question') or track.get('goal') or track.get('title', '')}"
        for track in experiment_blueprint.get("tracks", [])
    ) or "- 无"
    nodes = "\n".join(
        f"- {node.get('id', 'node')}: {node.get('summary') or node.get('label') or node.get('claim', '')}"
        for node in claim_graph.get("nodes", [])
    ) or "- 无"
    return (
        f"Title: {idea_brief.get('title', '')}\n"
        f"Problem: {idea_brief.get('problem', '')}\n"
        f"Novelty: {idea_brief.get('novelty_claim', '')}\n"
        f"Evidence Basis:\n{evidence_basis}\n"
        f"Claim Graph:\n{nodes}\n"
        f"Experiment Tracks:\n{tracks}\n"
    )


def _build_llm_round_report(
    idea_package: Mapping[str, Any],
    round_index: int,
    profile: RubricProfile,
    *,
    review_model: str,
    reviewer_count: int,
    client: Any,
    client_model: str,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    review = perform_review(
        _build_review_text(idea_package),
        model=client_model,
        client=client,
        num_reviews_ensemble=max(1, reviewer_count),
        num_reflections=1,
        num_fs_examples=0,
        temperature=0.3,
    )
    dimension_scores = {
        dimension.name: _dimension_score_from_review(dimension.name, review)
        for dimension in profile.dimensions
    }
    weighted = sum(dimension_scores[item.name] * item.weight for item in profile.dimensions)
    overall = _clamp_score(weighted)
    summary = str(review.get("Summary") or "").strip() or (
        f"第 {round_index} 轮 committee critique 完成，使用 review_model={review_model}。"
    )
    weaknesses = [str(item).strip() for item in review.get("Weaknesses", []) if str(item).strip()]
    limitations = [str(item).strip() for item in review.get("Limitations", []) if str(item).strip()]
    risks = weaknesses + [item for item in limitations if item not in weaknesses]
    report = {
        "round": round_index,
        "overall": overall,
        "summary": summary,
        "strengths": [str(item).strip() for item in review.get("Strengths", []) if str(item).strip()],
        "risks": risks,
        "open_questions": [str(item).strip() for item in review.get("Questions", []) if str(item).strip()] or ["委员会未提出额外问题。"],
        "dimension_scores": dimension_scores,
        "review_mode": "llm_committee",
    }
    blocking_issues = _collect_blocking_issues(report, profile.approval_threshold)
    recommendation = _recommendation_for_report(report, profile.approval_threshold)
    committee_decision_text = str(review.get("Decision") or "").strip()
    if recommendation != "go":
        committee_decision_text = recommendation
    committee_decision = {
        "status": "ok",
        "decision": committee_decision_text or recommendation,
        "overall": review.get("Overall"),
        "confidence": review.get("Confidence"),
        "review_model": review_model,
        "reviewer_count": max(1, reviewer_count),
        "blocking_issues": blocking_issues,
        "recommendation": recommendation,
    }
    return report, committee_decision


def run_critique_loop(
    idea_package: Mapping[str, Any],
    config: CritiqueLoopConfig | None = None,
) -> Dict[str, Any]:
    resolved_config = config or CritiqueLoopConfig()
    profile = resolved_config.rubric_profile or load_rubric_profile(resolved_config.rubric_name)
    working_package = deepcopy(dict(idea_package))
    rounds: list[Dict[str, Any]] = []
    final_report: Dict[str, Any] | None = None
    decision = "needs_revision"
    review_mode = resolved_config.review_mode
    committee_decision: Dict[str, Any] = {"status": "not_requested", "decision": None}
    client = None
    client_model = resolved_config.review_model

    if review_mode == "llm_committee":
        try:
            client, client_model = create_client(resolved_config.review_model)
        except Exception as exc:
            committee_decision = {
                "status": "llm_init_failed",
                "decision": None,
                "error": str(exc),
                "review_model": resolved_config.review_model,
                "reviewer_count": max(1, resolved_config.reviewer_count),
            }
            review_mode = "fallback_deterministic"

    for round_index in range(1, profile.max_rounds + 1):
        if review_mode == "llm_committee":
            try:
                round_report, committee_decision = _build_llm_round_report(
                    working_package,
                    round_index,
                    profile,
                    review_model=resolved_config.review_model,
                    reviewer_count=resolved_config.reviewer_count,
                    client=client,
                    client_model=client_model,
                )
            except Exception as exc:
                committee_decision = {
                    "status": "llm_failed",
                    "decision": None,
                    "error": str(exc),
                    "review_model": resolved_config.review_model,
                    "reviewer_count": max(1, resolved_config.reviewer_count),
                }
                review_mode = "fallback_deterministic"
                round_report = _build_round_report(working_package, round_index, profile)
        else:
            round_report = _build_round_report(working_package, round_index, profile)

        rounds.append(round_report)
        final_report = round_report

        if _decision_from_report(round_report, profile.approval_threshold) == "approved":
            decision = "approved"
            break

        working_package = revise_idea_package(working_package, round_report, round_index)

    critique_report = {
        "summary": final_report["summary"] if final_report else "未生成 critique 结果。",
        "strengths": list(final_report["strengths"] if final_report else []),
        "risks": list(final_report["risks"] if final_report else []),
        "open_questions": list(final_report["open_questions"] if final_report else []),
    }
    blocking_issues = _collect_blocking_issues(final_report or {}, profile.approval_threshold)
    recommendation = _recommendation_for_report(final_report or {}, profile.approval_threshold)
    decision = _decision_from_report(final_report or {}, profile.approval_threshold)

    return {
        "decision": decision,
        "rounds": rounds,
        "scorecard": {
            "overall": final_report["overall"] if final_report else 0.0,
            "threshold": profile.approval_threshold,
            "max_rounds": profile.max_rounds,
            "rubric_name": profile.name,
            "dimension_scores": dict(final_report["dimension_scores"] if final_report else {}),
            "review_mode": final_report["review_mode"] if final_report else review_mode,
            "blocking_issues": blocking_issues,
            "recommendation": recommendation,
            "committee_decision": dict(committee_decision),
        },
        "committee_decision": committee_decision,
        "critique_report": critique_report,
        "idea_package": working_package,
    }
