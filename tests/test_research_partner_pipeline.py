from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class ResearchPartnerPipelineTest(unittest.TestCase):
    def test_run_idea_pipeline_populates_core_outputs(self) -> None:
        from engine.research_partner.idea_pipeline import IdeaContext, run_idea_pipeline

        outputs = run_idea_pipeline(
            IdeaContext(
                title="Frequency-Domain Global Regression for Artifact Suppression",
                description="构建动态伪影抑制结合频率域全局回归的全色锐化网络架构，并设计消融实验。",
                evidence_basis=(
                    "高频细节恢复容易引入动态伪影。",
                    "频率域全局回归有助于保持结构一致性。",
                ),
            )
        )

        self.assertEqual(
            set(outputs),
            {"idea_brief", "claim_graph", "experiment_blueprint", "pipeline_notes", "revision_history", "evidence_context"},
        )
        self.assertEqual(outputs["idea_brief"]["title"], "Frequency-Domain Global Regression for Artifact Suppression")
        self.assertGreaterEqual(len(outputs["idea_brief"]["evidence_basis"]), 2)
        self.assertGreaterEqual(len(outputs["claim_graph"]["nodes"]), 3)
        track_ids = {track["id"] for track in outputs["experiment_blueprint"]["tracks"]}
        self.assertIn("core-hypothesis", track_ids)
        self.assertIn("ablation-plan", track_ids)
        self.assertEqual(outputs["revision_history"], [])

    def test_run_critique_loop_accepts_strong_idea_package(self) -> None:
        from engine.research_partner.critique_loop import CritiqueLoopConfig, run_critique_loop
        from engine.research_partner.idea_pipeline import IdeaContext, run_idea_pipeline
        from engine.research_partner.rubric_loader import RubricProfile, ScoringDimension

        idea_package = run_idea_pipeline(
            IdeaContext(
                title="Evidence-first Proposal Engine",
                description="利用结构化证据索引生成更可审校的研究提案。",
                evidence_basis=(
                    "已有 evidence index 设计可回链到具体文献。",
                    "Proposal-first 流程能够降低全文写作的幻觉风险。",
                    "多阶段 artifacts 便于人工审校与调试。",
                ),
            )
        )
        rubric_profile = RubricProfile(
            name="cvpr",
            approval_threshold=0.55,
            max_rounds=2,
            dimensions=(
                ScoringDimension(name="novelty", weight=0.4, description="Novelty"),
                ScoringDimension(name="methodology", weight=0.35, description="Methodology"),
                ScoringDimension(name="evidence_support", weight=0.25, description="Evidence"),
            ),
        )

        result = run_critique_loop(
            idea_package,
            CritiqueLoopConfig(rubric_profile=rubric_profile),
        )

        self.assertEqual(result["decision"], "approved")
        self.assertEqual(len(result["rounds"]), 1)
        self.assertIn("summary", result["critique_report"])
        self.assertIn("strengths", result["critique_report"])
        self.assertGreaterEqual(result["scorecard"]["overall"], 0.55)
        self.assertEqual(result["scorecard"]["recommendation"], "go")
        self.assertEqual(result["scorecard"]["blocking_issues"], [])
        self.assertIsNotNone(result["scorecard"]["committee_decision"])

    def test_run_critique_loop_uses_llm_committee_review_when_configured(self) -> None:
        from engine.research_partner.critique_loop import CritiqueLoopConfig, run_critique_loop
        from engine.research_partner.idea_pipeline import IdeaContext, run_idea_pipeline
        from engine.research_partner.rubric_loader import RubricProfile, ScoringDimension

        idea_package = run_idea_pipeline(
            IdeaContext(
                title="Committee-reviewed Proposal",
                description="利用 reviewer committee 对提案进行真实 LLM 评审。",
                evidence_basis=(
                    "已有结构化 evidence index。",
                    "已有 proposal critique loop。",
                    "已有可回链实验蓝图。",
                ),
            )
        )
        rubric_profile = RubricProfile(
            name="cvpr",
            approval_threshold=0.6,
            max_rounds=1,
            dimensions=(
                ScoringDimension(name="novelty", weight=0.4, description="Novelty"),
                ScoringDimension(name="methodology", weight=0.35, description="Methodology"),
                ScoringDimension(name="clarity", weight=0.25, description="Clarity"),
            ),
        )
        llm_review = {
            "Summary": "委员会认为该提案的问题定义清晰、证据链完整。",
            "Strengths": ["strength"],
            "Weaknesses": ["weakness"],
            "Questions": ["question"],
            "Limitations": ["limitation"],
            "Originality": 4,
            "Quality": 4,
            "Clarity": 4,
            "Significance": 3,
            "Ethical Concerns": False,
            "Soundness": 4,
            "Presentation": 3,
            "Contribution": 4,
            "Overall": 8,
            "Confidence": 4,
            "Decision": "Accept",
        }

        with patch(
            "engine.research_partner.critique_loop.create_client",
            return_value=(object(), "gpt-5.4-xhigh"),
        ) as client_mock, patch(
            "engine.research_partner.critique_loop.perform_review",
            return_value=llm_review,
        ) as review_mock:
            result = run_critique_loop(
                idea_package,
                CritiqueLoopConfig(
                    rubric_profile=rubric_profile,
                    review_mode="llm_committee",
                    review_model="gpt-5.4-xhigh",
                    reviewer_count=3,
                ),
            )

        client_mock.assert_called_once_with("gpt-5.4-xhigh")
        review_mock.assert_called_once()
        self.assertEqual(review_mock.call_args.kwargs["num_reviews_ensemble"], 3)
        self.assertEqual(result["scorecard"]["review_mode"], "llm_committee")
        self.assertEqual(result["scorecard"]["recommendation"], "revise")
        self.assertIn("weakness", result["scorecard"]["blocking_issues"])
        self.assertIn("limitation", result["scorecard"]["blocking_issues"])
        self.assertEqual(result["committee_decision"]["decision"], "revise")
        self.assertEqual(result["committee_decision"]["recommendation"], "revise")
        self.assertEqual(result["decision"], "needs_revision")
        self.assertIn("委员会认为该提案的问题定义清晰", result["critique_report"]["summary"])

    def test_run_critique_loop_falls_back_when_llm_committee_review_errors(self) -> None:
        from engine.research_partner.critique_loop import CritiqueLoopConfig, run_critique_loop
        from engine.research_partner.idea_pipeline import IdeaContext, run_idea_pipeline
        from engine.research_partner.rubric_loader import RubricProfile, ScoringDimension

        idea_package = run_idea_pipeline(
            IdeaContext(
                title="Fallback Proposal",
                description="利用 fallback critique 保证流程稳定。",
                evidence_basis=(
                    "已有结构化 evidence index。",
                    "已有 proposal critique loop。",
                    "已有可回链实验蓝图。",
                ),
            )
        )
        rubric_profile = RubricProfile(
            name="cvpr",
            approval_threshold=0.6,
            max_rounds=1,
            dimensions=(
                ScoringDimension(name="novelty", weight=0.4, description="Novelty"),
                ScoringDimension(name="methodology", weight=0.35, description="Methodology"),
                ScoringDimension(name="evidence_support", weight=0.25, description="Evidence"),
            ),
        )

        with patch(
            "engine.research_partner.critique_loop.create_client",
            return_value=(object(), "gpt-5.4-xhigh"),
        ), patch(
            "engine.research_partner.critique_loop.perform_review",
            side_effect=RuntimeError("llm unavailable"),
        ):
            result = run_critique_loop(
                idea_package,
                CritiqueLoopConfig(
                    rubric_profile=rubric_profile,
                    review_mode="llm_committee",
                    review_model="gpt-5.4-xhigh",
                ),
            )

        self.assertEqual(result["scorecard"]["review_mode"], "fallback_deterministic")
        self.assertEqual(result["scorecard"]["recommendation"], "go")
        self.assertEqual(result["scorecard"]["blocking_issues"], [])
        self.assertEqual(result["committee_decision"]["status"], "llm_failed")
        self.assertEqual(result["decision"], "approved")

    def test_run_critique_loop_falls_back_when_llm_client_init_errors(self) -> None:
        from engine.research_partner.critique_loop import CritiqueLoopConfig, run_critique_loop
        from engine.research_partner.idea_pipeline import IdeaContext, run_idea_pipeline
        from engine.research_partner.rubric_loader import RubricProfile, ScoringDimension

        idea_package = run_idea_pipeline(
            IdeaContext(
                title="Client Init Fallback Proposal",
                description="初始化 reviewer client 失败时回退到 deterministic critique。",
                evidence_basis=(
                    "已有结构化 evidence index。",
                    "已有 proposal critique loop。",
                    "已有可回链实验蓝图。",
                ),
            )
        )
        rubric_profile = RubricProfile(
            name="cvpr",
            approval_threshold=0.6,
            max_rounds=1,
            dimensions=(
                ScoringDimension(name="novelty", weight=0.4, description="Novelty"),
                ScoringDimension(name="methodology", weight=0.35, description="Methodology"),
                ScoringDimension(name="evidence_support", weight=0.25, description="Evidence"),
            ),
        )

        with patch(
            "engine.research_partner.critique_loop.create_client",
            side_effect=RuntimeError("missing api key"),
        ):
            result = run_critique_loop(
                idea_package,
                CritiqueLoopConfig(
                    rubric_profile=rubric_profile,
                    review_mode="llm_committee",
                    review_model="gpt-5.4-xhigh",
                ),
            )

        self.assertEqual(result["scorecard"]["review_mode"], "fallback_deterministic")
        self.assertEqual(result["scorecard"]["recommendation"], "go")
        self.assertEqual(result["scorecard"]["blocking_issues"], [])
        self.assertEqual(result["committee_decision"]["status"], "llm_init_failed")
        self.assertEqual(result["decision"], "approved")

    def test_build_review_text_prefers_claim_summary_and_track_question(self) -> None:
        from engine.research_partner.critique_loop import _build_review_text

        review_text = _build_review_text(
            {
                "idea_brief": {
                    "title": "Evidence-first Proposal",
                    "problem": "problem",
                    "novelty_claim": "novelty",
                    "evidence_basis": ["paper"],
                },
                "claim_graph": {
                    "nodes": [
                        {
                            "id": "method_hypothesis",
                            "label": "Evidence-first Method",
                            "summary": "通过结构化证据约束 reviewer 输入。",
                        },
                        {
                            "id": "expected_result",
                            "label": "Expected Result",
                        },
                    ],
                    "edges": [],
                },
                "experiment_blueprint": {
                    "tracks": [
                        {
                            "id": "core-hypothesis",
                            "question": "结构化证据是否提升 proposal 可审校性。",
                        }
                    ]
                },
            }
        )

        self.assertIn("method_hypothesis: 通过结构化证据约束 reviewer 输入。", review_text)
        self.assertIn("expected_result: Expected Result", review_text)
        self.assertIn("core-hypothesis: 结构化证据是否提升 proposal 可审校性。", review_text)

    def test_summarize_csv_supports_quoted_commas(self) -> None:
        from engine.research_partner.proposal_bundle import _summarize_csv

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "results.csv"
            csv_path.write_text(
                'model,notes,psnr\nbaseline,"stable, trusted",30.10\n"ours, v2","better, robust",30.52\n',
                encoding="utf-8",
            )

            summary, metrics = _summarize_csv(csv_path)

        self.assertEqual(summary, "ours, v2 notes better, robust (baseline stable, trusted)")
        self.assertEqual(
            metrics,
            [
                "ours, v2 notes better, robust (baseline stable, trusted)",
                "ours, v2 psnr 30.52 (baseline 30.10)",
            ],
        )

    def test_materialize_proposal_bundle_ingests_external_evidence_files(self) -> None:
        from engine.research_partner.proposal_bundle import materialize_proposal_bundle
        from engine.research_partner.rubric_loader import RubricProfile, ScoringDimension

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            (workspace / "workflow_idea.json").write_text(
                json.dumps(
                    {
                        "Name": "paper_writer_research_partner",
                        "Title": "Evidence-first topic",
                        "Experiment": "Use structured evidence intake to shape proposal generation.",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            (workspace / "workflow_state.json").write_text(
                json.dumps({"phase": "bootstrap_completed"}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            (workspace / "notes.txt").write_text("## Literature Snapshot\n", encoding="utf-8")
            pdf_path = workspace / "paper.pdf"
            csv_path = workspace / "results.csv"
            pdf_path.write_text("dummy pdf placeholder", encoding="utf-8")
            csv_path.write_text("model,psnr,ssim\nbaseline,30.10,0.910\nours,30.52,0.924\n", encoding="utf-8")

            idea_package = {
                "idea_brief": {
                    "title": "Evidence-first topic",
                    "problem": "problem",
                    "novelty_claim": "novelty",
                    "evidence_basis": ["paper", "csv"],
                },
                "claim_graph": {"nodes": [{"id": "n1"}], "edges": []},
                "experiment_blueprint": {"title": "Evidence-first topic", "tracks": []},
                "pipeline_notes": ["seeded from tests"],
                "revision_history": [],
                "evidence_context": {
                    "limitations": ["pdf limitation"],
                    "bottlenecks": ["pdf bottleneck"],
                    "metrics_summary": ["ours psnr 30.52"],
                    "sources": [
                        {"kind": "pdf", "path": str(pdf_path), "summary": "pdf summary"},
                        {"kind": "csv", "path": str(csv_path), "summary": "csv summary"},
                    ],
                },
            }
            critique_result = {
                "decision": "approved",
                "rounds": [{"round": 1, "overall": 0.8}],
                "scorecard": {"overall": 0.8},
                "critique_report": {
                    "summary": "summary",
                    "strengths": ["strength"],
                    "risks": ["risk"],
                    "open_questions": ["question"],
                },
                "idea_package": idea_package,
            }
            rubric_profile = RubricProfile(
                name="journal_q2",
                approval_threshold=0.58,
                max_rounds=2,
                dimensions=(
                    ScoringDimension(name="novelty", weight=0.3, description="Novelty"),
                    ScoringDimension(name="methodology", weight=0.3, description="Methodology"),
                    ScoringDimension(name="evidence_support", weight=0.2, description="Evidence"),
                    ScoringDimension(name="clarity", weight=0.2, description="Clarity"),
                ),
            )

            with patch(
                "engine.research_partner.proposal_bundle.ingest_evidence_context",
                return_value=(
                    (
                        "PDF summary evidence",
                        "CSV metrics evidence",
                    ),
                    {"limitations": ["pdf limitation"], "bottlenecks": ["pdf bottleneck"], "metrics_summary": ["ours psnr 30.52"], "sources": [{"kind": "pdf", "path": str(pdf_path), "summary": "pdf summary"}, {"kind": "csv", "path": str(csv_path), "summary": "csv summary"}]},
                    {"sources": [{"kind": "pdf", "path": str(pdf_path)}, {"kind": "csv", "path": str(csv_path)}]},
                ),
            ) as ingest_mock, patch(
                "engine.research_partner.proposal_bundle.run_idea_pipeline",
                return_value=idea_package,
            ) as pipeline_mock, patch(
                "engine.research_partner.proposal_bundle.run_critique_loop",
                return_value=critique_result,
            ), patch(
                "engine.research_partner.proposal_bundle.load_rubric_profile",
                return_value=rubric_profile,
            ):
                result = materialize_proposal_bundle(
                    workspace=workspace,
                    title="Evidence-first topic",
                    description="Use structured evidence intake to shape proposal generation.",
                    rubric_profile="journal_q2",
                    evidence_files=[str(pdf_path), str(csv_path)],
                )

            ingest_mock.assert_called_once()
            pipeline_context = pipeline_mock.call_args.args[0]
            self.assertEqual(tuple(pipeline_context.evidence_basis), ("PDF summary evidence", "CSV metrics evidence"))
            self.assertEqual(pipeline_context.evidence_context.metrics_summary[0], "ours psnr 30.52")
            evidence_index_path = workspace / "artifacts" / "research_partner" / "evidence_index.json"
            self.assertTrue(evidence_index_path.exists())
            evidence_index = json.loads(evidence_index_path.read_text(encoding="utf-8"))
            manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
            self.assertEqual(manifest["evidence_files"], ["paper.pdf", "results.csv"])
            self.assertIn("expected_figures", manifest["generated_files"])
            self.assertIn("rubric_scorecard", manifest["generated_files"])
            self.assertEqual(
                evidence_index["sources"],
                [{"kind": "pdf", "path": "paper.pdf"}, {"kind": "csv", "path": "results.csv"}],
            )
            self.assertIn("evidence_index", manifest["generated_files"])

    def test_materialize_proposal_bundle_rejects_evidence_files_outside_workspace(self) -> None:
        from engine.research_partner.proposal_bundle import materialize_proposal_bundle
        from engine.research_partner.rubric_loader import RubricProfile, ScoringDimension

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "workflow_idea.json").write_text(
                json.dumps(
                    {
                        "Name": "paper_writer_research_partner",
                        "Title": "Evidence-first topic",
                        "Experiment": "Use structured evidence intake to shape proposal generation.",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            (workspace / "workflow_state.json").write_text(
                json.dumps({"phase": "bootstrap_completed"}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            (workspace / "notes.txt").write_text("## Literature Snapshot\n", encoding="utf-8")
            external_pdf = root / "secret.pdf"
            external_pdf.write_text("dummy pdf placeholder", encoding="utf-8")

            idea_package = {
                "idea_brief": {
                    "title": "Evidence-first topic",
                    "problem": "problem",
                    "novelty_claim": "novelty",
                    "evidence_basis": ["paper"],
                },
                "claim_graph": {"nodes": [{"id": "n1"}], "edges": []},
                "experiment_blueprint": {"title": "Evidence-first topic", "tracks": []},
                "pipeline_notes": ["seeded from tests"],
                "revision_history": [],
            }
            critique_result = {
                "decision": "approved",
                "rounds": [{"round": 1, "overall": 0.8}],
                "scorecard": {"overall": 0.8},
                "critique_report": {
                    "summary": "summary",
                    "strengths": ["strength"],
                    "risks": ["risk"],
                    "open_questions": ["question"],
                },
                "idea_package": idea_package,
            }
            rubric_profile = RubricProfile(
                name="default",
                approval_threshold=0.58,
                max_rounds=2,
                dimensions=(
                    ScoringDimension(name="novelty", weight=0.3, description="Novelty"),
                    ScoringDimension(name="methodology", weight=0.3, description="Methodology"),
                    ScoringDimension(name="evidence_support", weight=0.2, description="Evidence"),
                    ScoringDimension(name="clarity", weight=0.2, description="Clarity"),
                ),
            )

            with patch(
                "engine.research_partner.proposal_bundle.run_idea_pipeline",
                return_value=idea_package,
            ), patch(
                "engine.research_partner.proposal_bundle.run_critique_loop",
                return_value=critique_result,
            ), patch(
                "engine.research_partner.proposal_bundle.load_rubric_profile",
                return_value=rubric_profile,
            ), self.assertRaises(ValueError) as ctx:
                materialize_proposal_bundle(
                    workspace=workspace,
                    title="Evidence-first topic",
                    description="Use structured evidence intake to shape proposal generation.",
                    evidence_files=[str(external_pdf)],
                )

            self.assertIn("workspace", str(ctx.exception))

    def test_materialize_proposal_bundle_redacts_absolute_evidence_paths_from_artifacts(self) -> None:
        from engine.research_partner.proposal_bundle import materialize_proposal_bundle
        from engine.research_partner.rubric_loader import RubricProfile, ScoringDimension

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            (workspace / "workflow_idea.json").write_text(
                json.dumps(
                    {
                        "Name": "paper_writer_research_partner",
                        "Title": "Evidence-first topic",
                        "Experiment": "Use structured evidence intake to shape proposal generation.",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            (workspace / "workflow_state.json").write_text(
                json.dumps({"phase": "bootstrap_completed"}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            (workspace / "notes.txt").write_text("## Literature Snapshot\n", encoding="utf-8")
            evidence_dir = workspace / "evidence"
            evidence_dir.mkdir()
            pdf_path = evidence_dir / "paper.pdf"
            csv_path = evidence_dir / "results.csv"
            pdf_path.write_text("dummy pdf placeholder", encoding="utf-8")
            csv_path.write_text("model,psnr,ssim\nbaseline,30.10,0.910\nours,30.52,0.924\n", encoding="utf-8")

            idea_package = {
                "idea_brief": {
                    "title": "Evidence-first topic",
                    "problem": "problem",
                    "novelty_claim": "novelty",
                    "evidence_basis": ["paper", "csv"],
                },
                "claim_graph": {"nodes": [{"id": "n1"}], "edges": []},
                "experiment_blueprint": {"title": "Evidence-first topic", "tracks": []},
                "pipeline_notes": ["seeded from tests"],
                "revision_history": [],
            }
            critique_result = {
                "decision": "approved",
                "rounds": [{"round": 1, "overall": 0.8}],
                "scorecard": {"overall": 0.8},
                "critique_report": {
                    "summary": "summary",
                    "strengths": ["strength"],
                    "risks": ["risk"],
                    "open_questions": ["question"],
                },
                "idea_package": idea_package,
            }
            rubric_profile = RubricProfile(
                name="cvpr",
                approval_threshold=0.58,
                max_rounds=2,
                dimensions=(
                    ScoringDimension(name="novelty", weight=0.3, description="Novelty"),
                    ScoringDimension(name="methodology", weight=0.3, description="Methodology"),
                    ScoringDimension(name="evidence_support", weight=0.2, description="Evidence"),
                    ScoringDimension(name="clarity", weight=0.2, description="Clarity"),
                ),
            )

            with patch(
                "engine.research_partner.proposal_bundle.run_idea_pipeline",
                return_value=idea_package,
            ), patch(
                "engine.research_partner.proposal_bundle.run_critique_loop",
                return_value=critique_result,
            ), patch(
                "engine.research_partner.proposal_bundle.load_rubric_profile",
                return_value=rubric_profile,
            ):
                result = materialize_proposal_bundle(
                    workspace=workspace,
                    title="Evidence-first topic",
                    description="Use structured evidence intake to shape proposal generation.",
                    evidence_files=[str(pdf_path), str(csv_path)],
                )

            manifest_path = Path(result["manifest_path"])
            evidence_index_path = workspace / "artifacts" / "research_partner" / "evidence_index.json"
            manifest_text = manifest_path.read_text(encoding="utf-8")
            evidence_index_text = evidence_index_path.read_text(encoding="utf-8")
            self.assertNotIn(str(pdf_path.resolve()), manifest_text)
            self.assertNotIn(str(csv_path.resolve()), manifest_text)
            self.assertNotIn(str(pdf_path.resolve()), evidence_index_text)
            self.assertNotIn(str(csv_path.resolve()), evidence_index_text)

            manifest = json.loads(manifest_text)
            evidence_index = json.loads(evidence_index_text)
            self.assertEqual(manifest["workspace"], ".")
            self.assertEqual(
                manifest["source_files"],
                {
                    "workflow_idea": "workflow_idea.json",
                    "workflow_state": "workflow_state.json",
                    "notes": "notes.txt",
                },
            )
            self.assertEqual(
                manifest["generated_files"],
                {
                    "idea_brief": "artifacts/research_partner/idea_brief.json",
                    "claim_graph": "artifacts/research_partner/claim_graph.json",
                    "critique_report": "artifacts/research_partner/critique_report.json",
                    "proposal_brief": "artifacts/research_partner/proposal_brief.md",
                    "experiment_blueprint": "artifacts/research_partner/experiment_blueprint.json",
                    "expected_figures": "artifacts/research_partner/expected_figures.json",
                    "rubric_scorecard": "artifacts/research_partner/rubric_scorecard.json",
                    "evidence_index": "artifacts/research_partner/evidence_index.json",
                },
            )
            self.assertEqual(manifest["evidence_files"], ["evidence/paper.pdf", "evidence/results.csv"])
            self.assertIn("expected_figures", manifest["generated_files"])
            self.assertIn("rubric_scorecard", manifest["generated_files"])
            self.assertEqual(
                evidence_index["sources"],
                [{"kind": "pdf", "path": "evidence/paper.pdf"}, {"kind": "csv", "path": "evidence/results.csv"}],
            )

    def test_materialize_proposal_bundle_delegates_to_pipeline_and_critique_loop(self) -> None:
        from engine.research_partner.proposal_bundle import materialize_proposal_bundle
        from engine.research_partner.rubric_loader import RubricProfile, ScoringDimension

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            (workspace / "workflow_idea.json").write_text(
                json.dumps(
                    {
                        "Name": "paper_writer_research_partner",
                        "Title": "Evidence-first topic",
                        "Experiment": "Use structured evidence intake to shape proposal generation.",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            (workspace / "workflow_state.json").write_text(
                json.dumps({"phase": "bootstrap_completed"}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            (workspace / "notes.txt").write_text(
                "\n".join(
                    [
                        "## Literature Snapshot",
                        "1. Evidence Graphs for Research Planning (2025, ICML) - Ada Smith",
                    ]
                ),
                encoding="utf-8",
            )

            idea_package = {
                "idea_brief": {
                    "title": "Evidence-first topic",
                    "problem": "problem",
                    "novelty_claim": "novelty",
                    "evidence_basis": ["paper"],
                },
                "claim_graph": {"nodes": [{"id": "n1"}], "edges": []},
                "experiment_blueprint": {"title": "Evidence-first topic", "tracks": []},
                "pipeline_notes": ["seeded from tests"],
                "revision_history": [],
            }
            critique_result = {
                "decision": "approved",
                "rounds": [{"round": 1, "overall": 0.8}],
                "scorecard": {"overall": 0.8},
                "critique_report": {
                    "summary": "summary",
                    "strengths": ["strength"],
                    "risks": ["risk"],
                    "open_questions": ["question"],
                },
                "idea_package": idea_package,
            }
            rubric_profile = RubricProfile(
                name="journal_q2",
                approval_threshold=0.58,
                max_rounds=2,
                dimensions=(
                    ScoringDimension(name="novelty", weight=0.3, description="Novelty"),
                    ScoringDimension(name="methodology", weight=0.3, description="Methodology"),
                    ScoringDimension(name="evidence_support", weight=0.2, description="Evidence"),
                    ScoringDimension(name="clarity", weight=0.2, description="Clarity"),
                ),
            )

            with patch(
                "engine.research_partner.proposal_bundle.run_idea_pipeline",
                return_value=idea_package,
            ) as pipeline_mock, patch(
                "engine.research_partner.proposal_bundle.run_critique_loop",
                return_value=critique_result,
            ) as critique_mock, patch(
                "engine.research_partner.proposal_bundle.load_rubric_profile",
                return_value=rubric_profile,
            ) as rubric_mock:
                materialize_proposal_bundle(
                    workspace=workspace,
                    title="Evidence-first topic",
                    description="Use structured evidence intake to shape proposal generation.",
                    rubric_profile="journal_q2",
                )

            pipeline_mock.assert_called_once()
            critique_mock.assert_called_once()
            rubric_mock.assert_called_once_with("journal_q2")
            critique_config = critique_mock.call_args.args[1]
            self.assertEqual(critique_config.rubric_profile.name, "journal_q2")


if __name__ == "__main__":
    unittest.main()
