from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


class RubricLoaderTest(unittest.TestCase):
    def test_loads_valid_venue_yaml_as_typed_profile(self) -> None:
        from engine.research_partner.rubric_loader import RubricProfile, load_rubric_profile

        with tempfile.TemporaryDirectory() as tmpdir:
            rubrics_dir = Path(tmpdir)
            (rubrics_dir / "cvpr.yaml").write_text(
                "\n".join(
                    [
                        "name: cvpr",
                        "approval_threshold: 0.72",
                        "max_rounds: 3",
                        "dimensions:",
                        "  - name: novelty",
                        "    weight: 0.4",
                        "    description: Novel contribution",
                        "  - name: methodology",
                        "    weight: 0.35",
                        "    description: Technical soundness",
                        "  - name: evidence_support",
                        "    weight: 0.25",
                        "    description: Evidence quality",
                    ]
                ),
                encoding="utf-8",
            )

            profile = load_rubric_profile("cvpr", rubrics_dir=rubrics_dir)

            self.assertIsInstance(profile, RubricProfile)
            self.assertEqual(profile.name, "cvpr")
            self.assertEqual(profile.max_rounds, 3)
            self.assertAlmostEqual(sum(item.weight for item in profile.dimensions), 1.0)
            self.assertEqual(profile.dimensions[0].name, "novelty")

    def test_missing_venue_gracefully_falls_back_to_default(self) -> None:
        from engine.research_partner.rubric_loader import load_rubric_profile

        with tempfile.TemporaryDirectory() as tmpdir:
            rubrics_dir = Path(tmpdir)
            (rubrics_dir / "default.yaml").write_text(
                "\n".join(
                    [
                        "name: default",
                        "approval_threshold: 0.6",
                        "max_rounds: 2",
                        "dimensions:",
                        "  - name: novelty",
                        "    weight: 0.5",
                        "    description: Baseline novelty",
                        "  - name: feasibility",
                        "    weight: 0.5",
                        "    description: Baseline feasibility",
                    ]
                ),
                encoding="utf-8",
            )

            profile = load_rubric_profile("neurips", rubrics_dir=rubrics_dir)

            self.assertEqual(profile.name, "default")
            self.assertAlmostEqual(profile.approval_threshold, 0.6)

    def test_duplicate_dimension_names_raise_schema_validation_error(self) -> None:
        from engine.research_partner.rubric_loader import SchemaValidationError, load_rubric_profile

        with tempfile.TemporaryDirectory() as tmpdir:
            rubrics_dir = Path(tmpdir)
            (rubrics_dir / "default.yaml").write_text(
                "\n".join(
                    [
                        "name: default",
                        "approval_threshold: 0.6",
                        "max_rounds: 2",
                        "dimensions:",
                        "  - name: novelty",
                        "    weight: 0.5",
                        "    description: First novelty",
                        "  - name: novelty",
                        "    weight: 0.5",
                        "    description: Duplicate novelty",
                    ]
                ),
                encoding="utf-8",
            )

            with self.assertRaises(SchemaValidationError):
                load_rubric_profile("default", rubrics_dir=rubrics_dir)

        from engine.research_partner.rubric_loader import SchemaValidationError, load_rubric_profile

        with tempfile.TemporaryDirectory() as tmpdir:
            rubrics_dir = Path(tmpdir)
            (rubrics_dir / "default.yaml").write_text(
                "\n".join(
                    [
                        "name: default",
                        "approval_threshold: 0.6",
                        "max_rounds: 2",
                        "dimensions:",
                        "  - name: novelty",
                        "    weight: 0.7",
                        "    description: Baseline novelty",
                        "  - name: feasibility",
                        "    weight: 0.1",
                        "    description: Baseline feasibility",
                    ]
                ),
                encoding="utf-8",
            )

            with self.assertRaises(SchemaValidationError):
                load_rubric_profile("default", rubrics_dir=rubrics_dir)


if __name__ == "__main__":
    unittest.main()
