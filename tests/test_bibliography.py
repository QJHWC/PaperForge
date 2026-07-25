from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from engine.bibliography import (
    bibliography_summary,
    export_bibtex,
    import_bibtex,
    record_writeup_citations,
    section_entries,
)
from mcp_servers.runner import invoke_server

BIBTEX_SAMPLE = """
@article{lock2024,
  title = {Lock-Aware Writing Pipelines},
  author = {Smith, Ada and Doe, Ben},
  year = {2024},
  journal = {Systems Journal},
  doi = {10.1000/lock-aware}
}

@article{lock2024dup,
  title = {Lock-Aware Writing Pipelines},
  author = {Smith, Ada and Doe, Ben},
  year = {2024},
  journal = {Systems Journal},
  doi = {10.1000/lock-aware}
}

@inproceedings{bridge2025,
  title = {Bridging Workflow Agents and Contracts},
  author = {Lee, Carol},
  year = {2025},
  booktitle = {BridgeConf}
}
""".strip()


class BibliographyTest(unittest.TestCase):
    def test_import_export_and_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            result = import_bibtex(workspace, BIBTEX_SAMPLE)
            self.assertEqual(result["imported_count"], 2)
            self.assertEqual(result["duplicate_count"], 1)

            summary = bibliography_summary(workspace)
            self.assertEqual(summary["entry_count"], 2)

            exported = export_bibtex(workspace)
            self.assertIn("@article{lock2024", exported)
            self.assertIn("@inproceedings{bridge2025", exported)

            mapping = record_writeup_citations(workspace, "Related Work", ["lock2024", "bridge2025"])
            self.assertEqual(mapping["section"], "Related Work")

            related_entries = section_entries(workspace, "Related Work")
            self.assertEqual(len(related_entries), 2)

    def test_bibliography_mcp_server_bridge(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            import_bibtex(workspace, BIBTEX_SAMPLE)
            result = invoke_server(
                "bibliography",
                tool="summary",
                payload={"workspace": str(workspace)},
            )
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["summary"]["entry_count"], 2)


if __name__ == "__main__":
    unittest.main()
