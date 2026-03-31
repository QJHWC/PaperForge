from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from agents.coordinator import PaperForgeCoordinator
from agents.mvp_workflow_agent import MvpWorkflowAgent
from agents.scientist_workflow_agent import ScientistWorkflowAgent
from agents.writeup_agent import WriteupAgent
from launch_user_entry import (
    ROOT,
    _build_entry_cmd,
    _build_mvp_cmd,
    _build_research_cmd,
    _maybe_materialize_research_partner,
    build_parser,
    main,
)


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

    def test_coordinator_routes_research_partner_as_safe_mvp_bootstrap(self) -> None:
        result = PaperForgeCoordinator().run("research_partner", experiment="paper_writer", rubric_profile="cvpr")
        self.assertEqual(result.mode, "research_partner")
        self.assertEqual(result.payload["status"], "planned")
        self.assertEqual(result.payload["mode_behavior"]["forced_phase"], "bootstrap")
        self.assertNotIn("phase", result.payload["input"])
        self.assertIn("--phase", result.payload["command"])
        self.assertIn("bootstrap", result.payload["command"])
        self.assertIn("--rubric-profile", result.payload["command"])
        self.assertIn("cvpr", result.payload["command"])
        self.assertIn("--skip-mvp-run", result.payload["command"])
        self.assertIn("--skip-writeup", result.payload["command"])
        self.assertIn("--refresh-literature", result.payload["command"])

    def test_status_snapshot_includes_research_partner_mode(self) -> None:
        payload = PaperForgeCoordinator().route_frontend_action("status_snapshot")
        self.assertIn("research_partner", payload["payload"]["modes"])
        self.assertIn("research_partner", payload["payload"]["schemas"]["coordinator"]["properties"]["mode"]["enum"])
        self.assertIn("research_partner", payload["payload"]["schemas"])

    def test_status_snapshot_exposes_dedicated_research_partner_schema(self) -> None:
        payload = PaperForgeCoordinator().route_frontend_action("status_snapshot")
        schema = payload["payload"]["schemas"]["research_partner"]
        self.assertEqual(schema["required"], ["experiment"])
        self.assertIn("literature_top_k", schema["properties"])
        self.assertIn("rubric_profile", schema["properties"])
        self.assertIn("evidence_files", schema["properties"])
        self.assertIn("execute", schema["properties"])
        self.assertNotIn("phase", schema["properties"])
        self.assertNotIn("skip_writeup", schema["properties"])
        self.assertNotIn("skip_mvp_run", schema["properties"])
        self.assertNotIn("refresh_literature", schema["properties"])
        self.assertNotIn("cloud_run_dir", schema["properties"])
        self.assertNotEqual(schema, payload["payload"]["schemas"]["mvp"])

    def test_status_snapshot_exposes_research_partner_behavior_metadata(self) -> None:
        payload = PaperForgeCoordinator().route_frontend_action("status_snapshot")
        behavior = payload["payload"]["mode_behaviors"]["research_partner"]
        self.assertEqual(behavior["delegates_to"], "mvp")
        self.assertEqual(behavior["forced_phase"], "bootstrap")
        self.assertEqual(behavior["artifact_mode"], "planned_only")
        self.assertEqual(
            behavior["planned_output_keys"],
            [
                "idea_brief",
                "claim_graph",
                "critique_report",
                "proposal_brief",
                "experiment_blueprint",
                "expected_figures",
                "rubric_scorecard",
                "evidence_index",
                "manifest",
            ],
        )

    def test_research_partner_payload_uses_snapshot_behavior_metadata(self) -> None:
        coordinator = PaperForgeCoordinator()
        result = coordinator.run("research_partner", experiment="paper_writer")
        snapshot = coordinator.status_snapshot()
        self.assertEqual(result.payload["mode_behavior"], snapshot["mode_behaviors"]["research_partner"])

    def test_status_snapshot_exposes_research_partner_planned_workflow(self) -> None:
        payload = PaperForgeCoordinator().route_frontend_action("status_snapshot")
        workflow = payload["payload"]["planned_workflows"]["research_partner"]
        self.assertEqual([stage["id"] for stage in workflow], ["search", "idea", "critique"])
        self.assertEqual(workflow[0]["status"], "planned")
        self.assertEqual(workflow[1]["planned_outputs"], ["idea_brief", "claim_graph", "experiment_blueprint"])
        self.assertEqual(
            workflow[2]["planned_outputs"],
            [
                "critique_report",
                "proposal_brief",
                "expected_figures",
                "rubric_scorecard",
                "evidence_index",
                "manifest",
            ],
        )

    def test_research_partner_payload_uses_snapshot_planned_workflow(self) -> None:
        coordinator = PaperForgeCoordinator()
        result = coordinator.run("research_partner", experiment="paper_writer")
        snapshot = coordinator.status_snapshot()
        self.assertEqual(result.payload["planned_workflow"], snapshot["planned_workflows"]["research_partner"])

    def test_frontend_research_partner_route_matches_direct_planned_workflow(self) -> None:
        coordinator = PaperForgeCoordinator()
        routed = coordinator.route_frontend_action(
            "run_research_partner",
            {
                "experiment": "paper_writer",
                "run_dir": "results/paper_writer/demo",
            },
        )
        direct = coordinator.run(
            "research_partner",
            experiment="paper_writer",
            run_dir="results/paper_writer/demo",
        )
        self.assertEqual(routed["payload"]["planned_workflow"], direct.payload["planned_workflow"])

    def test_status_snapshot_exposes_research_partner_contract_version(self) -> None:
        payload = PaperForgeCoordinator().route_frontend_action("status_snapshot")
        self.assertEqual(payload["payload"]["contract_versions"]["research_partner"], "v1")

    def test_research_partner_payload_uses_snapshot_contract_version(self) -> None:
        coordinator = PaperForgeCoordinator()
        result = coordinator.run("research_partner", experiment="paper_writer")
        snapshot = coordinator.status_snapshot()
        self.assertEqual(result.payload["contract_version"], snapshot["contract_versions"]["research_partner"])

    def test_frontend_research_partner_route_matches_direct_contract_version(self) -> None:
        coordinator = PaperForgeCoordinator()
        routed = coordinator.route_frontend_action(
            "run_research_partner",
            {
                "experiment": "paper_writer",
                "run_dir": "results/paper_writer/demo",
            },
        )
        direct = coordinator.run(
            "research_partner",
            experiment="paper_writer",
            run_dir="results/paper_writer/demo",
        )
        self.assertEqual(routed["payload"]["contract_version"], direct.payload["contract_version"])

    def test_frontend_research_partner_route_converges_conflicting_inputs(self) -> None:
        payload = PaperForgeCoordinator().route_frontend_action(
            "run_research_partner",
            {
                "experiment": "paper_writer",
                "run_dir": "results/paper_writer/demo",
                "engine": "openalex",
                "title": "Evidence-first topic",
                "description": "desc",
                "idea_name": "demo_idea",
                "literature_top_k": 7,
                "rubric_profile": "cvpr",
                "evidence_files": ["/tmp/paper.pdf", "/tmp/results.csv"],
                "phase": "cloud",
                "skip_mvp_run": False,
                "skip_writeup": False,
                "refresh_literature": False,
            },
        )
        self.assertTrue(payload["accepted"])
        self.assertEqual(payload["message"], "Research partner workflow request routed.")
        result = payload["payload"]
        self.assertEqual(result["status"], "planned")
        self.assertIn("agent", result)
        self.assertIn("entrypoint", result)
        self.assertIn("command", result)
        self.assertIn("input_schema", result)
        self.assertIn("trace", result)
        self.assertIn("artifacts", result)
        self.assertEqual(result["mode_behavior"]["forced_phase"], "bootstrap")
        self.assertEqual(result["input"]["run_dir"], "results/paper_writer/demo")
        self.assertEqual(result["input"]["title"], "Evidence-first topic")
        self.assertEqual(result["input"]["description"], "desc")
        self.assertEqual(result["input"]["idea_name"], "demo_idea")
        self.assertEqual(result["input"]["literature_top_k"], 7)
        self.assertEqual(result["input"]["rubric_profile"], "cvpr")
        self.assertEqual(result["input"]["evidence_files"], ["paper.pdf", "results.csv"])
        self.assertNotIn("phase", result["input"])
        self.assertNotIn("skip_mvp_run", result["input"])
        self.assertNotIn("skip_writeup", result["input"])
        self.assertNotIn("refresh_literature", result["input"])
        self.assertIn("--phase", result["command"])
        self.assertIn("bootstrap", result["command"])
        self.assertIn("--skip-mvp-run", result["command"])
        self.assertIn("--skip-writeup", result["command"])
        self.assertIn("--refresh-literature", result["command"])

    def test_research_partner_payload_input_matches_public_schema_fields(self) -> None:
        result = PaperForgeCoordinator().run(
            "research_partner",
            experiment="paper_writer",
            run_dir="results/paper_writer/demo",
            title="Evidence-first topic",
            description="desc",
            idea_name="demo_idea",
            literature_top_k=7,
        )
        self.assertEqual(
            set(result.payload["input"]),
            {
                "experiment",
                "run_dir",
                "engine",
                "idea_name",
                "title",
                "description",
                "literature_top_k",
                "rubric_profile",
                "evidence_files",
                "execute",
            },
        )

    def test_research_partner_payload_redacts_absolute_run_dir_from_public_input(self) -> None:
        workspace = Path("/tmp/demo/workspace").resolve()
        result = PaperForgeCoordinator().run(
            "research_partner",
            experiment="paper_writer",
            run_dir=str(workspace),
        )
        self.assertEqual(result.payload["workspace"], ".")
        self.assertEqual(result.payload["input"]["run_dir"], ".")
        self.assertNotIn("/tmp/demo", result.payload["input"]["run_dir"])

    def test_research_partner_planned_outputs_use_relative_artifact_paths_for_resolved_workspace(self) -> None:
        result = PaperForgeCoordinator().run(
            "research_partner",
            experiment="paper_writer",
            run_dir="results/paper_writer/demo",
        )
        self.assertEqual(result.payload["workspace"], ".")
        planned_outputs = result.payload["planned_outputs"]
        self.assertEqual(
            planned_outputs["idea_brief"]["path"],
            "artifacts/research_partner/idea_brief.json",
        )
        self.assertEqual(
            planned_outputs["critique_report"]["path"],
            "artifacts/research_partner/critique_report.json",
        )

    def test_research_partner_planned_outputs_omit_path_when_workspace_is_unresolved(self) -> None:
        result = PaperForgeCoordinator().run(
            "research_partner",
            experiment="paper_writer",
        )
        planned_outputs = result.payload["planned_outputs"]
        self.assertIsNone(result.payload["workspace"])
        self.assertIsNone(planned_outputs["idea_brief"]["path"])
        self.assertIsNone(planned_outputs["claim_graph"]["path"])
        self.assertIsNone(planned_outputs["critique_report"]["path"])
        self.assertIsNone(planned_outputs["expected_figures"]["path"])
        self.assertIsNone(planned_outputs["rubric_scorecard"]["path"])

    def test_research_partner_planned_outputs_include_structured_artifacts(self) -> None:
        result = PaperForgeCoordinator().run(
            "research_partner",
            experiment="paper_writer",
            run_dir="results/paper_writer/demo",
        )
        planned_outputs = result.payload["planned_outputs"]
        self.assertEqual(
            set(planned_outputs),
            {
                "idea_brief",
                "claim_graph",
                "critique_report",
                "proposal_brief",
                "experiment_blueprint",
                "expected_figures",
                "rubric_scorecard",
                "evidence_index",
                "manifest",
            },
        )
        self.assertEqual(planned_outputs["idea_brief"]["format"], "json")
        self.assertEqual(planned_outputs["claim_graph"]["status"], "planned")
        self.assertEqual(planned_outputs["proposal_brief"]["format"], "markdown")
        self.assertEqual(planned_outputs["expected_figures"]["format"], "json")
        self.assertEqual(planned_outputs["rubric_scorecard"]["status"], "planned")
        self.assertEqual(
            planned_outputs["critique_report"]["path"],
            "artifacts/research_partner/critique_report.json",
        )

    def test_status_snapshot_exposes_research_partner_output_contracts(self) -> None:
        payload = PaperForgeCoordinator().route_frontend_action("status_snapshot")
        contracts = payload["payload"]["output_contracts"]["research_partner"]
        self.assertEqual(set(contracts["required"]), {"idea_brief", "claim_graph", "critique_report"})
        self.assertEqual(
            contracts["properties"]["idea_brief"]["required"],
            ["title", "problem", "novelty_claim", "evidence_basis"],
        )
        self.assertEqual(contracts["properties"]["claim_graph"]["required"], ["nodes", "edges"])
        self.assertEqual(
            contracts["properties"]["critique_report"]["required"],
            ["summary", "strengths", "risks", "open_questions"],
        )
        self.assertEqual(contracts["properties"]["expected_figures"]["required"], ["figures"])
        self.assertEqual(
            contracts["properties"]["rubric_scorecard"]["required"],
            [
                "overall",
                "threshold",
                "max_rounds",
                "rubric_name",
                "dimension_scores",
                "review_mode",
                "blocking_issues",
                "recommendation",
                "committee_decision",
            ],
        )

    def test_status_snapshot_exposes_nested_research_partner_field_types(self) -> None:
        payload = PaperForgeCoordinator().route_frontend_action("status_snapshot")
        contracts = payload["payload"]["output_contracts"]["research_partner"]
        self.assertEqual(contracts["properties"]["idea_brief"]["properties"]["title"]["type"], "string")
        self.assertEqual(contracts["properties"]["idea_brief"]["properties"]["evidence_basis"]["type"], "array")
        self.assertEqual(contracts["properties"]["claim_graph"]["properties"]["nodes"]["type"], "array")
        self.assertEqual(contracts["properties"]["critique_report"]["properties"]["summary"]["type"], "string")
        self.assertEqual(contracts["properties"]["critique_report"]["properties"]["risks"]["type"], "array")
        self.assertEqual(contracts["properties"]["expected_figures"]["properties"]["figures"]["type"], "array")
        self.assertEqual(contracts["properties"]["rubric_scorecard"]["properties"]["overall"]["type"], "number")
        self.assertEqual(contracts["properties"]["rubric_scorecard"]["properties"]["blocking_issues"]["type"], "array")

    def test_research_partner_payload_uses_snapshot_output_contracts(self) -> None:
        coordinator = PaperForgeCoordinator()
        result = coordinator.run(
            "research_partner",
            experiment="paper_writer",
            run_dir="results/paper_writer/demo",
        )
        snapshot = coordinator.status_snapshot()
        self.assertEqual(
            result.payload["planned_output_contracts"],
            snapshot["output_contracts"]["research_partner"],
        )

    def test_research_partner_planned_outputs_embed_contract_skeletons(self) -> None:
        result = PaperForgeCoordinator().run(
            "research_partner",
            experiment="paper_writer",
            run_dir="results/paper_writer/demo",
        )
        planned_outputs = result.payload["planned_outputs"]
        self.assertEqual(
            planned_outputs["idea_brief"]["skeleton"],
            {
                "title": "",
                "problem": "",
                "novelty_claim": "",
                "evidence_basis": [],
            },
        )
        self.assertEqual(planned_outputs["claim_graph"]["skeleton"], {"nodes": [], "edges": []})
        self.assertEqual(
            planned_outputs["critique_report"]["skeleton"],
            {
                "summary": "",
                "strengths": [],
                "risks": [],
                "open_questions": [],
            },
        )
        self.assertEqual(planned_outputs["proposal_brief"]["skeleton"], "")
        self.assertEqual(
            planned_outputs["experiment_blueprint"]["skeleton"],
            {
                "title": "",
                "tracks": [],
            },
        )
        self.assertEqual(planned_outputs["expected_figures"]["skeleton"], {"figures": []})
        self.assertEqual(
            planned_outputs["rubric_scorecard"]["skeleton"],
            {
                "overall": 0.0,
                "threshold": 0.0,
                "max_rounds": 0,
                "rubric_name": "",
                "dimension_scores": {},
                "review_mode": "",
                "blocking_issues": [],
                "recommendation": "revise",
                "committee_decision": None,
            },
        )
        self.assertEqual(planned_outputs["evidence_index"]["skeleton"], {"sources": []})
        self.assertEqual(planned_outputs["manifest"]["skeleton"]["workspace"], ".")
        self.assertEqual(planned_outputs["manifest"]["skeleton"]["critique_decision"], "needs_revision")

    def test_frontend_research_partner_route_matches_direct_output_contracts(self) -> None:
        coordinator = PaperForgeCoordinator()
        routed = coordinator.route_frontend_action(
            "run_research_partner",
            {
                "experiment": "paper_writer",
                "run_dir": "results/paper_writer/demo",
            },
        )
        direct = coordinator.run(
            "research_partner",
            experiment="paper_writer",
            run_dir="results/paper_writer/demo",
        )
        self.assertEqual(
            routed["payload"]["planned_output_contracts"],
            direct.payload["planned_output_contracts"],
        )
        self.assertEqual(
            set(routed["payload"]["planned_outputs"]),
            set(routed["payload"]["planned_output_contracts"]["properties"]),
        )

    def test_frontend_research_partner_route_matches_direct_output_skeletons(self) -> None:
        coordinator = PaperForgeCoordinator()
        routed = coordinator.route_frontend_action(
            "run_research_partner",
            {
                "experiment": "paper_writer",
                "run_dir": "results/paper_writer/demo",
            },
        )
        direct = coordinator.run(
            "research_partner",
            experiment="paper_writer",
            run_dir="results/paper_writer/demo",
        )
        self.assertEqual(
            routed["payload"]["planned_outputs"]["idea_brief"]["skeleton"],
            direct.payload["planned_outputs"]["idea_brief"]["skeleton"],
        )
        self.assertEqual(
            routed["payload"]["planned_outputs"]["claim_graph"]["skeleton"],
            direct.payload["planned_outputs"]["claim_graph"]["skeleton"],
        )

    def test_frontend_research_partner_route_matches_direct_planned_outputs(self) -> None:
        coordinator = PaperForgeCoordinator()
        routed = coordinator.route_frontend_action(
            "run_research_partner",
            {
                "experiment": "paper_writer",
                "run_dir": "results/paper_writer/demo",
            },
        )
        direct = coordinator.run(
            "research_partner",
            experiment="paper_writer",
            run_dir="results/paper_writer/demo",
        )
        self.assertEqual(routed["payload"]["planned_outputs"], direct.payload["planned_outputs"])

    def test_existing_bridges_do_not_expose_research_partner_planned_outputs(self) -> None:
        self.assertNotIn("planned_outputs", MvpWorkflowAgent().run(phase="bootstrap", experiment="paper_writer"))
        self.assertNotIn("planned_outputs", ScientistWorkflowAgent().run(experiment="paper_writer"))
        self.assertNotIn("planned_outputs", WriteupAgent().run(workflow_kind="mvp", workspace="workspace/demo"))

    def test_build_entry_cmd_routes_research_partner_explicitly(self) -> None:
        args = SimpleNamespace(entry="research_partner")
        self.assertIs(_build_entry_cmd(args), _build_research_cmd)

    def test_build_entry_cmd_rejects_unknown_entry(self) -> None:
        args = SimpleNamespace(entry="unknown")
        with self.assertRaises(ValueError):
            _build_entry_cmd(args)

    def test_user_entry_builds_research_partner_bootstrap_command(self) -> None:
        parser = build_parser()
        args = parser.parse_args([
            "research_partner",
            "--experiment",
            "paper_writer",
            "--title",
            "Evidence-first topic",
            "--rubric-profile",
            "journal_q2",
            "--evidence-file",
            "/tmp/paper.pdf",
            "--evidence-file",
            "/tmp/results.csv",
        ])
        command = _build_research_cmd(args, [])
        self.assertIn("launch_mvp_workflow.py", " ".join(command))
        self.assertIn("--phase", command)
        self.assertIn("bootstrap", command)
        self.assertIn("--skip-mvp-run", command)
        self.assertIn("--skip-writeup", command)
        self.assertIn("--refresh-literature", command)
        self.assertIn("--rubric-profile", command)
        self.assertIn("journal_q2", command)
        self.assertEqual(command.count("--evidence-file"), 2)
        self.assertIn("/tmp/paper.pdf", command)
        self.assertIn("/tmp/results.csv", command)

    def test_research_partner_subparser_accepts_rubric_profile_choices(self) -> None:
        parser = build_parser()
        args = parser.parse_args([
            "research_partner",
            "--rubric-profile",
            "cvpr",
            "--evidence-file",
            "/tmp/paper.pdf",
            "--evidence-file",
            "/tmp/results.csv",
        ])
        self.assertEqual(args.rubric_profile, "cvpr")
        self.assertEqual(args.evidence_file, ["/tmp/paper.pdf", "/tmp/results.csv"])

    def test_research_partner_subparser_rejects_unknown_rubric_profile(self) -> None:
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args([
                "research_partner",
                "--rubric-profile",
                "neurips",
            ])

        parser = build_parser()
        args, passthrough = parser.parse_known_args([
            "research_partner",
            "--experiment",
            "paper_writer",
            "--writeup-model",
            "claude-sonnet-4-6",
        ])
        with self.assertRaises(SystemExit) as ctx:
            _build_research_cmd(args, passthrough)
        self.assertIn("research_partner 不支持额外参数", str(ctx.exception))

    def test_user_entry_help_describes_research_partner_as_bootstrap_execution_entry(self) -> None:
        parser = build_parser()
        help_text = parser.format_help()
        self.assertIn("research_partner", help_text)
        self.assertIn("bootstrap", help_text)
        self.assertIn("会执行轻量 bootstrap", help_text)

    def test_research_partner_subparser_help_discloses_bootstrap_execution_behavior(self) -> None:
        parser = build_parser()
        stdout = io.StringIO()
        with self.assertRaises(SystemExit):
            with redirect_stdout(stdout):
                parser.parse_args(["research_partner", "--help"])
        help_text = stdout.getvalue()
        self.assertIn("bootstrap", help_text)
        self.assertIn("会执行轻量 bootstrap", help_text)

    def test_user_entry_materializes_research_partner_after_bootstrap(self) -> None:
        completed = SimpleNamespace(
            returncode=0,
            stdout="[bootstrap] workspace=/tmp/demo\n[done] workspace=/tmp/demo\n",
            stderr="",
        )
        with unittest.mock.patch("launch_user_entry.materialize_proposal_bundle") as materialize_mock:
            _maybe_materialize_research_partner(
                entry="research_partner",
                args=SimpleNamespace(
                    title="Evidence-first topic",
                    description="desc",
                    rubric_profile="cvpr",
                    evidence_file=["/tmp/paper.pdf", "/tmp/results.csv"],
                ),
                completed=completed,
            )
        materialize_mock.assert_called_once()
        self.assertEqual(materialize_mock.call_args.kwargs["rubric_profile"], "cvpr")
        self.assertEqual(materialize_mock.call_args.kwargs["evidence_files"], ["/tmp/paper.pdf", "/tmp/results.csv"])

    def test_research_partner_main_dry_run_prints_command_without_credentials(self) -> None:
        stdout = io.StringIO()

        with unittest.mock.patch.dict("launch_user_entry.os.environ", {}, clear=True), unittest.mock.patch.object(
            sys,
            "argv",
            [
                "launch_user_entry.py",
                "research_partner",
                "--dry-run",
                "--experiment",
                "paper_writer",
                "--rubric-profile",
                "journal_q2",
            ],
        ), unittest.mock.patch("launch_user_entry._require_virtualenv"):
            with redirect_stdout(stdout):
                main()

        stdout_text = stdout.getvalue()
        self.assertIn("[entry] research_partner", stdout_text)
        self.assertIn("[command]", stdout_text)
        self.assertIn("--rubric-profile journal_q2", stdout_text)
        self.assertIn("--skip-mvp-run", stdout_text)
        self.assertIn("--skip-writeup", stdout_text)
        self.assertIn("--refresh-literature", stdout_text)

    def test_research_partner_main_redacts_displayed_paths_but_preserves_materialization_inputs(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()

        class StreamingProcess:
            def __init__(self) -> None:
                self.stdout = iter([
                    "[done] workspace=/tmp/demo\n",
                    "artifact=/tmp/demo/artifacts/research_partner\n",
                ])
                self.stderr = iter(["warn path=/tmp/demo/logs/run.log\n"])
                self.returncode = 0

            def wait(self) -> int:
                return self.returncode

        with unittest.mock.patch.object(
            sys,
            "argv",
            [
                "launch_user_entry.py",
                "research_partner",
                "--experiment",
                "paper_writer",
                "--rubric-profile",
                "journal_q2",
                "--evidence-file",
                "/tmp/paper.pdf",
                "--evidence-file",
                "/tmp/results.csv",
            ],
        ), unittest.mock.patch("launch_user_entry._require_virtualenv"), unittest.mock.patch(
            "launch_user_entry.subprocess.Popen",
            return_value=StreamingProcess(),
        ) as popen_mock, unittest.mock.patch(
            "launch_user_entry.subprocess.run"
        ) as run_mock, unittest.mock.patch(
            "launch_user_entry.materialize_proposal_bundle"
        ) as materialize_mock:
            with self.assertRaises(SystemExit) as exit_ctx:
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    main()

        self.assertEqual(exit_ctx.exception.code, 0)
        run_mock.assert_not_called()
        command = popen_mock.call_args.args[0]
        self.assertEqual(command[command.index("--rubric-profile") + 1], "journal_q2")
        self.assertEqual(
            [command[index + 1] for index, token in enumerate(command) if token == "--evidence-file"],
            ["/tmp/paper.pdf", "/tmp/results.csv"],
        )
        stdout_text = stdout.getvalue()
        stderr_text = stderr.getvalue()
        self.assertNotIn("/tmp/demo", stdout_text)
        self.assertNotIn("/tmp/demo", stderr_text)
        self.assertIn("[done] workspace=", stdout_text)
        self.assertEqual(materialize_mock.call_args.kwargs["workspace"], Path("/tmp/demo").resolve())
        self.assertEqual(materialize_mock.call_args.kwargs["rubric_profile"], "journal_q2")
        self.assertEqual(materialize_mock.call_args.kwargs["evidence_files"], ["/tmp/paper.pdf", "/tmp/results.csv"])

    def test_user_entry_materialize_research_partner_rejects_outside_workspace_evidence(self) -> None:
        completed = SimpleNamespace(
            returncode=0,
            stdout="[bootstrap] workspace=/tmp/demo\n[done] workspace=/tmp/demo\n",
            stderr="",
        )
        with self.assertRaises(SystemExit) as ctx:
            _maybe_materialize_research_partner(
                entry="research_partner",
                args=SimpleNamespace(
                    title="Evidence-first topic",
                    description="desc",
                    rubric_profile="cvpr",
                    evidence_file=["/etc/passwd"],
                ),
                completed=completed,
            )
        self.assertIn("workspace", str(ctx.exception))

    def test_user_entry_materializes_from_run_dir_when_done_line_missing(self) -> None:
        completed = SimpleNamespace(
            returncode=0,
            stdout="[bootstrap] no explicit done marker\n",
            stderr="",
        )
        with unittest.mock.patch("launch_user_entry.materialize_proposal_bundle") as materialize_mock:
            _maybe_materialize_research_partner(
                entry="research_partner",
                args=SimpleNamespace(
                    run_dir="results/paper_writer/demo",
                    title="Evidence-first topic",
                    description="desc",
                    rubric_profile="cvpr",
                    evidence_file=["/tmp/paper.pdf"],
                ),
                completed=completed,
            )
        materialize_mock.assert_called_once()
        self.assertEqual(
            materialize_mock.call_args.kwargs["workspace"],
            (ROOT / "results/paper_writer/demo").resolve(),
        )

    def test_user_entry_materialization_prefers_explicit_run_dir_over_done_line(self) -> None:
        completed = SimpleNamespace(
            returncode=0,
            stdout="[done] workspace=/tmp/demo\n",
            stderr="",
        )
        with unittest.mock.patch("launch_user_entry.materialize_proposal_bundle") as materialize_mock:
            _maybe_materialize_research_partner(
                entry="research_partner",
                args=SimpleNamespace(
                    run_dir="results/paper_writer/demo",
                    title="Evidence-first topic",
                    description="desc",
                    rubric_profile="cvpr",
                    evidence_file=[],
                ),
                completed=completed,
            )
        materialize_mock.assert_called_once()
        self.assertEqual(
            materialize_mock.call_args.kwargs["workspace"],
            (ROOT / "results/paper_writer/demo").resolve(),
        )

    def test_user_entry_streams_sanitized_child_output_and_preserves_raw_materialization_workspace(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()

        class StreamingProcess:
            def __init__(self) -> None:
                self.stdout = iter([
                    "[bootstrap] workspace=/tmp/demo/workspace\\n",
                    "[done] workspace=/tmp/demo/workspace\\n",
                ])
                self.stderr = iter(["warn path=/tmp/demo/workspace/logs/run.log\\n"])
                self.returncode = 0

            def wait(self) -> int:
                return self.returncode

        with unittest.mock.patch.object(
            sys,
            "argv",
            [
                "launch_user_entry.py",
                "research_partner",
                "--experiment",
                "paper_writer",
                "--rubric-profile",
                "journal_q2",
            ],
        ), unittest.mock.patch("launch_user_entry._require_virtualenv"), unittest.mock.patch(
            "launch_user_entry.subprocess.Popen",
            return_value=StreamingProcess(),
        ), unittest.mock.patch(
            "launch_user_entry.materialize_proposal_bundle"
        ) as materialize_mock:
            with self.assertRaises(SystemExit) as exit_ctx:
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    main()

        self.assertEqual(exit_ctx.exception.code, 0)
        stdout_text = stdout.getvalue()
        stderr_text = stderr.getvalue()
        self.assertNotIn("/tmp/demo/workspace", stdout_text)
        self.assertNotIn("/tmp/demo/workspace", stderr_text)
        self.assertIn("workspace=workspace", stdout_text)
        self.assertIn("path=run.log", stderr_text)
        self.assertEqual(materialize_mock.call_args.kwargs["workspace"], Path("/tmp/demo/workspace").resolve())

    def test_user_entry_uses_subprocess_run_for_non_research_partner_entries(self) -> None:
        stdout = io.StringIO()
        completed = SimpleNamespace(returncode=0, stdout="done\n", stderr="")
        with unittest.mock.patch.object(
            sys,
            "argv",
            ["launch_user_entry.py", "mvp", "--phase", "bootstrap"],
        ), unittest.mock.patch("launch_user_entry._require_virtualenv"), unittest.mock.patch(
            "launch_user_entry.subprocess.run",
            return_value=completed,
        ) as run_mock, unittest.mock.patch(
            "launch_user_entry.subprocess.Popen"
        ) as popen_mock:
            with self.assertRaises(SystemExit) as exit_ctx:
                with redirect_stdout(stdout):
                    main()

        self.assertEqual(exit_ctx.exception.code, 0)
        run_mock.assert_called_once()
        popen_mock.assert_not_called()

    def test_research_partner_dry_run_skips_credential_validation(self) -> None:
        stdout = io.StringIO()
        empty_cfg = {
            "openai_api_key": {"value": None, "source": "default"},
            "openai_base_url": {"value": None, "source": "default"},
            "openai_writeup_api_key": {"value": None, "source": "default"},
            "openai_writeup_base_url": {"value": None, "source": "default"},
            "anthropic_api_key": {"value": None, "source": "default"},
            "anthropic_auth_token": {"value": None, "source": "default"},
            "anthropic_base_url": {"value": None, "source": "default"},
            "writeup_cite_rounds": {"value": None, "source": "default"},
            "writeup_latex_fix_rounds": {"value": None, "source": "default"},
            "writeup_second_refinement": {"value": None, "source": "default"},
        }

        with unittest.mock.patch.object(
            sys,
            "argv",
            ["launch_user_entry.py", "research_partner", "--dry-run"],
        ), unittest.mock.patch("launch_user_entry._require_virtualenv"), unittest.mock.patch(
            "launch_user_entry._collect_effective_config",
            return_value=empty_cfg,
        ), unittest.mock.patch(
            "launch_user_entry._validate_effective_config",
            side_effect=AssertionError("dry-run should skip credential validation"),
        ) as validate_mock, unittest.mock.patch(
            "launch_user_entry._apply_api_env"
        ) as apply_env_mock, unittest.mock.patch(
            "launch_user_entry.subprocess.Popen"
        ) as popen_mock, unittest.mock.patch(
            "launch_user_entry.subprocess.run"
        ) as run_mock:
            with redirect_stdout(stdout):
                main()

        validate_mock.assert_not_called()
        apply_env_mock.assert_not_called()
        popen_mock.assert_not_called()
        run_mock.assert_not_called()
        self.assertIn("launch_mvp_workflow.py", stdout.getvalue())

    def test_research_partner_execute_passes_rubric_profile_to_materializer(self) -> None:
        coordinator = PaperForgeCoordinator()
        coordinator.mvp.run = Mock(
            return_value={
                "agent": "MvpWorkflowAgent",
                "entrypoint": "launch_mvp_workflow.py",
                "status": "completed",
                "input_schema": {"type": "object"},
                "input": {},
                "command": ["python", "launch_mvp_workflow.py"],
                "workspace": str(Path("results/paper_writer/demo").resolve()),
                "trace": [],
                "artifacts": [],
                "stdout": "",
                "stderr": "",
                "returncode": 0,
            }
        )

        with unittest.mock.patch("agents.coordinator.materialize_proposal_bundle") as materialize_mock:
            coordinator.run(
                "research_partner",
                experiment="paper_writer",
                run_dir="results/paper_writer/demo",
                rubric_profile="journal_q2",
                execute=True,
            )

        materialize_mock.assert_called_once()
        self.assertEqual(materialize_mock.call_args.kwargs["rubric_profile"], "journal_q2")

    def test_materialize_proposal_bundle_writes_extended_research_partner_artifacts(self) -> None:
        from engine.research_partner.proposal_bundle import materialize_proposal_bundle

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
                        "# Title: Evidence-first topic",
                        "# Experiment description: Use structured evidence intake to shape proposal generation.",
                        "",
                        "## Literature Snapshot",
                        "Query: `Evidence-first topic`",
                        "",
                        "1. Evidence Graphs for Research Planning (2025, ICML) - Ada Smith",
                        "2. Proposal Critique Loops (2024, NeurIPS) - Ben Lee",
                    ]
                ),
                encoding="utf-8",
            )

            materialize_proposal_bundle(
                workspace=workspace,
                title="Evidence-first topic",
                description="Use structured evidence intake to shape proposal generation.",
            )

            artifacts_dir = workspace / "artifacts" / "research_partner"
            self.assertTrue((artifacts_dir / "idea_brief.json").exists())
            self.assertTrue((artifacts_dir / "claim_graph.json").exists())
            self.assertTrue((artifacts_dir / "critique_report.json").exists())
            self.assertTrue((artifacts_dir / "proposal_brief.md").exists())
            self.assertTrue((artifacts_dir / "experiment_blueprint.json").exists())
            self.assertTrue((artifacts_dir / "expected_figures.json").exists())
            self.assertTrue((artifacts_dir / "rubric_scorecard.json").exists())
            self.assertTrue((artifacts_dir / "evidence_index.json").exists())

            manifest = json.loads((artifacts_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["rubric_profile"], "default")
            self.assertIn("expected_figures", manifest["generated_files"])
            self.assertIn("rubric_scorecard", manifest["generated_files"])

            idea_brief = json.loads((artifacts_dir / "idea_brief.json").read_text(encoding="utf-8"))
            self.assertEqual(
                set(idea_brief),
                {"title", "problem", "novelty_claim", "evidence_basis"},
            )
            self.assertGreaterEqual(len(idea_brief["evidence_basis"]), 1)

            claim_graph = json.loads((artifacts_dir / "claim_graph.json").read_text(encoding="utf-8"))
            self.assertEqual(set(claim_graph), {"nodes", "edges"})
            self.assertGreaterEqual(len(claim_graph["nodes"]), 2)

            critique_report = json.loads((artifacts_dir / "critique_report.json").read_text(encoding="utf-8"))
            self.assertEqual(
                set(critique_report),
                {"summary", "strengths", "risks", "open_questions"},
            )

            expected_figures = json.loads((artifacts_dir / "expected_figures.json").read_text(encoding="utf-8"))
            self.assertEqual(set(expected_figures), {"figures"})
            self.assertGreaterEqual(len(expected_figures["figures"]), 1)

            rubric_scorecard = json.loads((artifacts_dir / "rubric_scorecard.json").read_text(encoding="utf-8"))
            self.assertIn("overall", rubric_scorecard)
            self.assertIn("blocking_issues", rubric_scorecard)
            self.assertIn("recommendation", rubric_scorecard)
            self.assertIn("committee_decision", rubric_scorecard)

    def test_research_partner_execute_redacts_stdout_stderr_paths(self) -> None:
        coordinator = PaperForgeCoordinator()
        coordinator.mvp.run = Mock(
            return_value={
                "agent": "MvpWorkflowAgent",
                "entrypoint": "launch_mvp_workflow.py",
                "status": "completed",
                "input_schema": {"type": "object"},
                "input": {},
                "command": ["python", "launch_mvp_workflow.py"],
                "workspace": str(Path("/tmp/demo/workspace").resolve()),
                "trace": [],
                "artifacts": [],
                "stdout": "[done] workspace=/tmp/demo/workspace\nartifact=/tmp/demo/workspace/artifacts/research_partner\n",
                "stderr": "warn path=/tmp/demo/workspace/logs/run.log\n",
                "returncode": 0,
            }
        )

        result = coordinator.run(
            "research_partner",
            experiment="paper_writer",
            run_dir="results/paper_writer/demo",
            execute=True,
        )

        self.assertNotIn("/tmp/demo", result.payload["stdout"])
        self.assertNotIn("/tmp/demo", result.payload["stderr"])
        self.assertIn("workspace=.", result.payload["stdout"])
        self.assertIn("artifact=artifacts/research_partner", result.payload["stdout"])
        self.assertIn("path=logs/run.log", result.payload["stderr"])

    def test_research_partner_execute_sanitizes_materialization_value_error(self) -> None:
        coordinator = PaperForgeCoordinator()
        coordinator.mvp.run = Mock(
            return_value={
                "agent": "MvpWorkflowAgent",
                "entrypoint": "launch_mvp_workflow.py",
                "status": "completed",
                "input_schema": {"type": "object"},
                "input": {},
                "command": ["python", "launch_mvp_workflow.py"],
                "workspace": str(Path("/tmp/demo/workspace").resolve()),
                "trace": [],
                "artifacts": [],
                "stdout": "",
                "stderr": "",
                "returncode": 0,
            }
        )

        with unittest.mock.patch(
            "agents.coordinator.materialize_proposal_bundle",
            side_effect=ValueError("evidence file must stay within workspace: /tmp/demo/secret.pdf"),
        ):
            result = coordinator.run(
                "research_partner",
                experiment="paper_writer",
                run_dir="results/paper_writer/demo",
                execute=True,
            )

        self.assertEqual(result.payload["status"], "failed")
        self.assertEqual(result.payload["workspace"], ".")
        self.assertEqual(result.payload["returncode"], 0)
        self.assertIn("proposal_materialize", [item["stage"] for item in result.payload["trace"]])
        materialize_trace = next(item for item in result.payload["trace"] if item["stage"] == "proposal_materialize")
        self.assertEqual(materialize_trace["status"], "error")
        self.assertIn("workspace", materialize_trace["detail"])
        self.assertNotIn("/tmp/demo", materialize_trace["detail"])

    def test_research_partner_execute_sanitizes_unexpected_materialization_error(self) -> None:
        coordinator = PaperForgeCoordinator()
        coordinator.mvp.run = Mock(
            return_value={
                "agent": "MvpWorkflowAgent",
                "entrypoint": "launch_mvp_workflow.py",
                "status": "completed",
                "input_schema": {"type": "object"},
                "input": {},
                "command": ["python", "launch_mvp_workflow.py"],
                "workspace": str(Path("/tmp/demo/workspace").resolve()),
                "trace": [],
                "artifacts": [],
                "stdout": "",
                "stderr": "",
                "returncode": 0,
            }
        )

        with unittest.mock.patch(
            "agents.coordinator.materialize_proposal_bundle",
            side_effect=RuntimeError("internal path /tmp/demo/workspace leaked"),
        ):
            result = coordinator.run(
                "research_partner",
                experiment="paper_writer",
                run_dir="results/paper_writer/demo",
                execute=True,
            )

        self.assertEqual(result.payload["status"], "failed")
        materialize_trace = next(item for item in result.payload["trace"] if item["stage"] == "proposal_materialize")
        self.assertEqual(materialize_trace["status"], "error")
        self.assertIn("research_partner materialization failed", materialize_trace["detail"])
        self.assertNotIn("/tmp/demo", materialize_trace["detail"])

    def test_research_partner_execute_marks_planned_outputs_completed_after_materialization(self) -> None:
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
                        "# Title: Evidence-first topic",
                        "# Experiment description: Use structured evidence intake to shape proposal generation.",
                        "",
                        "## Literature Snapshot",
                        "Query: `Evidence-first topic`",
                        "",
                        "1. Evidence Graphs for Research Planning (2025, ICML) - Ada Smith",
                    ]
                ),
                encoding="utf-8",
            )

            coordinator = PaperForgeCoordinator()
            coordinator.mvp.run = Mock(
                return_value={
                    "agent": "MvpWorkflowAgent",
                    "entrypoint": "launch_mvp_workflow.py",
                    "status": "completed",
                    "input_schema": {"type": "object"},
                    "input": {},
                    "command": ["python", "launch_mvp_workflow.py"],
                    "workspace": str(workspace),
                    "trace": [],
                    "artifacts": [],
                    "stdout": "",
                    "stderr": "",
                    "returncode": 0,
                }
            )

            result = coordinator.run(
                "research_partner",
                experiment="paper_writer",
                run_dir=str(workspace),
                title="Evidence-first topic",
                description="Use structured evidence intake to shape proposal generation.",
                execute=True,
            )

            self.assertEqual(result.payload["status"], "completed")
            self.assertEqual(result.payload["planned_outputs"]["idea_brief"]["status"], "completed")
            self.assertEqual(result.payload["planned_outputs"]["claim_graph"]["status"], "completed")
            self.assertEqual(result.payload["planned_outputs"]["critique_report"]["status"], "completed")
            self.assertEqual(result.payload["planned_outputs"]["proposal_brief"]["status"], "completed")
            self.assertEqual(result.payload["planned_outputs"]["experiment_blueprint"]["status"], "completed")
            self.assertEqual(result.payload["planned_outputs"]["expected_figures"]["status"], "completed")
            self.assertEqual(result.payload["planned_outputs"]["rubric_scorecard"]["status"], "completed")
            self.assertEqual(result.payload["planned_outputs"]["evidence_index"]["status"], "completed")
            self.assertEqual(result.payload["planned_outputs"]["manifest"]["status"], "completed")
            artifact_paths = {item["path"] for item in result.payload["artifacts"]}
            self.assertIn(
                "artifacts/research_partner/proposal_brief.md",
                artifact_paths,
            )
            self.assertIn(
                "artifacts/research_partner/experiment_blueprint.json",
                artifact_paths,
            )
            self.assertIn(
                "artifacts/research_partner/expected_figures.json",
                artifact_paths,
            )
            self.assertIn(
                "artifacts/research_partner/rubric_scorecard.json",
                artifact_paths,
            )
            self.assertIn(
                "artifacts/research_partner/evidence_index.json",
                artifact_paths,
            )
            self.assertIn(
                "artifacts/research_partner/manifest.json",
                artifact_paths,
            )

