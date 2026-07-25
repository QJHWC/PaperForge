from __future__ import annotations

import unittest


class IdeaPipelineTest(unittest.TestCase):
    def test_run_idea_pipeline_consumes_pdf_csv_source_metadata(self) -> None:
        from engine.research_partner.idea_pipeline import (
            EvidenceContext,
            IdeaContext,
            run_idea_pipeline,
        )

        outputs = run_idea_pipeline(
            IdeaContext(
                title="Frequency-Domain Global Regression for Artifact Suppression",
                description="构建动态伪影抑制结合频率域全局回归的全色锐化网络架构，并设计消融实验。",
                evidence_basis=(
                    "高频细节恢复容易引入动态伪影。",
                    "CSV 指标显示方法在 PSNR 上优于基线。",
                ),
                evidence_context=EvidenceContext(
                    limitations=(
                        "现有方法在跨场景泛化时容易出现伪影累积。",
                    ),
                    bottlenecks=(
                        "局部卷积难以稳定建模全局频率一致性。",
                    ),
                    metrics_summary=(
                        "PSNR +0.42dB over baseline on validation split.",
                    ),
                    sources=(
                        {"kind": "pdf", "path": "/tmp/paper.pdf", "summary": "提出频率域全局回归并报告伪影抑制优势。"},
                        {"kind": "csv", "path": "/tmp/results.csv", "summary": "验证集上 PSNR/SSIM 均优于基线。"},
                    ),
                ),
            )
        )

        self.assertIn("metrics_summary", outputs["evidence_context"])
        self.assertEqual(outputs["evidence_context"]["metrics_summary"][0], "PSNR +0.42dB over baseline on validation split.")
        self.assertEqual(len(outputs["evidence_context"]["sources"]), 2)
        self.assertIn("metrics=1", outputs["pipeline_notes"])
        self.assertIn("sources=2", outputs["pipeline_notes"])
        self.assertIn("PSNR +0.42dB", outputs["idea_brief"]["problem"])
        self.assertEqual(outputs["claim_graph"]["nodes"][0]["evidence_refs"], ["evidence:1", "evidence:2"])

    def test_run_idea_pipeline_consumes_structured_evidence_context(self) -> None:
        from engine.research_partner.idea_pipeline import (
            EvidenceContext,
            IdeaContext,
            run_idea_pipeline,
        )

        outputs = run_idea_pipeline(
            IdeaContext(
                title="Frequency-Domain Global Regression for Artifact Suppression",
                description="构建动态伪影抑制结合频率域全局回归的全色锐化网络架构，并设计消融实验。",
                evidence_basis=(
                    "高频细节恢复容易引入动态伪影。",
                    "频率域全局回归有助于保持结构一致性。",
                ),
                evidence_context=EvidenceContext(
                    limitations=(
                        "现有方法在跨场景泛化时容易出现伪影累积。",
                    ),
                    bottlenecks=(
                        "局部卷积难以稳定建模全局频率一致性。",
                    ),
                ),
            )
        )

        self.assertIn("evidence_context", outputs)
        self.assertEqual(outputs["evidence_context"]["limitations"][0], "现有方法在跨场景泛化时容易出现伪影累积。")
        self.assertEqual(outputs["evidence_context"]["bottlenecks"][0], "局部卷积难以稳定建模全局频率一致性。")
        self.assertIn("limitations=1", outputs["pipeline_notes"])
        self.assertIn("bottlenecks=1", outputs["pipeline_notes"])
        self.assertTrue(
            "瓶颈" in outputs["idea_brief"]["problem"] or "局限" in outputs["idea_brief"]["problem"],
        )


if __name__ == "__main__":
    unittest.main()
