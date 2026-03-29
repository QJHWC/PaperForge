from __future__ import annotations

import unittest

from agents.coordinator import PaperForgeCoordinator
from agents.mvp_workflow_agent import MvpWorkflowAgent
from agents.scientist_workflow_agent import ScientistWorkflowAgent
from agents.writeup_agent import WriteupAgent


class AgentBridgeTest(unittest.TestCase):
    def test_mvp_agent_returns_planned_contract(self) -> None:
        result = MvpWorkflowAgent().run(phase="bootstrap", experiment="paper_writer", execute=False)
        self.assertEqual(result["status"], "planned")
        self.assertIn("launch_mvp_workflow.py", " ".join(result["command"]))
        self.assertIn("trace", result)
        self.assertIn("artifacts", result)
        self.assertEqual(result["input_schema"]["type"], "object")

    def test_scientist_agent_returns_planned_contract(self) -> None:
        result = ScientistWorkflowAgent().run(experiment="paper_writer", num_ideas=2, execute=False)
        self.assertEqual(result["status"], "planned")
        self.assertIn("launch_scientist.py", " ".join(result["command"]))
        self.assertEqual(result["input"]["num_ideas"], 2)

    def test_writeup_agent_exposes_phase_skill_map(self) -> None:
        result = WriteupAgent().run(workflow_kind="mvp", workspace="workspace/demo", execute=False)
        self.assertEqual(result["status"], "planned")
        self.assertIn("phase_skill_map", result["input"])
        self.assertIn("init", result["input"]["phase_skill_map"])

    def test_coordinator_routes_status_snapshot(self) -> None:
        payload = PaperForgeCoordinator().route_frontend_action("status_snapshot")
        self.assertTrue(payload["accepted"])
        self.assertIn("schemas", payload["payload"])
        self.assertIn("modes", payload["payload"])


if __name__ == "__main__":
    unittest.main()
