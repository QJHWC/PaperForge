from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SmokeTest(unittest.TestCase):
    def test_frontend_is_app_js_driven(self) -> None:
        html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="app"', html)
        self.assertIn('src="./app.js"', html)
        self.assertNotIn("Frontend action accepted by coordinator skeleton", html)

    def test_skill_contract_files_exist(self) -> None:
        required_skills = [
            "write-section",
            "refine-section",
            "citation-gap",
            "citation-select",
            "latex-fix",
            "de-aigc-rewrite",
        ]
        for skill_id in required_skills:
            skill_dir = ROOT / "skills" / skill_id
            self.assertTrue((skill_dir / "SKILL.md").exists(), skill_id)
            self.assertTrue((skill_dir / "schema.json").exists(), skill_id)
            self.assertTrue((skill_dir / "example_input.json").exists(), skill_id)
            self.assertTrue((skill_dir / "example_output.json").exists(), skill_id)

    def test_mcp_server_skeletons_exist(self) -> None:
        required_servers = [
            "literature",
            "bibliography",
            "file-gateway",
            "remote-runner",
            "diagram",
            "aigc-eval",
        ]
        for server_name in required_servers:
            self.assertTrue((ROOT / "mcp_servers" / server_name / "server.py").exists(), server_name)
        self.assertTrue((ROOT / "mcp_servers" / "runner.py").exists())

    def test_readme_mentions_current_layers(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for token in ["agents/", "frontend/", "mcp_servers/", "skills/", "tests/"]:
            self.assertIn(token, readme)


if __name__ == "__main__":
    unittest.main()
