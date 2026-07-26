from __future__ import annotations

import json
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import frontend.server as server_mod
from frontend.process_manager import ProcessManager


def _start_server() -> tuple[ThreadingHTTPServer, int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), server_mod.Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


def _multipart_body(parts: list[tuple[str, str, bytes]], boundary: str = "----PaperForgeBoundary"):
    chunks: list[bytes] = []
    for field, filename, content in parts:
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(
            (
                f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n"
            ).encode()
        )
        chunks.append(content)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), boundary


class FrontendApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.results_dir = self.root / "results"
        self.workspace = self.results_dir / "paper_writer" / "demo"
        self.workspace.mkdir(parents=True)
        (self.workspace / "workflow_state.json").write_text(
            json.dumps({"phase": "refine_completed", "status": "completed"}),
            encoding="utf-8",
        )
        (self.workspace / "notes.txt").write_text("notes", encoding="utf-8")

        self.patcher_results = patch.object(server_mod, "RESULTS_DIR", self.results_dir.resolve())
        self.patcher_root = patch.object(server_mod, "ROOT", self.root.resolve())
        self.patcher_pm = patch.object(
            server_mod,
            "PROCESS_MANAGER",
            ProcessManager(root=self.root.resolve(), results_dir=self.results_dir.resolve()),
        )
        self.patcher_results.start()
        self.patcher_root.start()
        self.patcher_pm.start()
        self.server, self.port = _start_server()
        self.csrf_token, self.session_cookie = self._establish_session()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.patcher_pm.stop()
        self.patcher_root.stop()
        self.patcher_results.stop()
        self.tmp.cleanup()

    def _establish_session(self) -> tuple[str, str]:
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/session")
        resp = conn.getresponse()
        payload = json.loads(resp.read())
        cookie = resp.getheader("Set-Cookie", "").split(";", 1)[0]
        conn.close()
        return payload["csrf_token"], cookie

    def _request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict | None = None,
        *,
        authenticated: bool = True,
    ):
        request_headers = dict(headers or {})
        if authenticated and method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
            request_headers.setdefault("X-PaperForge-CSRF", self.csrf_token)
            request_headers.setdefault("Cookie", self.session_cookie)
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, body=body, headers=request_headers)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return resp.status, raw

    def _request_json(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        *,
        authenticated: bool = True,
        headers: dict | None = None,
    ):
        payload = json.dumps(body or {}).encode("utf-8")
        request_headers = {"Content-Type": "application/json", **(headers or {})}
        return self._request(
            method,
            path,
            payload,
            request_headers,
            authenticated=authenticated,
        )

    def test_put_workspace_config_persists_file(self) -> None:
        status, raw = self._request_json(
            "PUT",
            "/api/workspace/paper_writer/demo/config",
            {
                "writeup_model": "gpt-5.4-xhigh",
                "gateway_profile": "full",
                "existing_draft": "drafts/demo.tex",
                "skip_chktex_fix": False,
                "active_source_draft_id": "draft_1",
            },
        )
        self.assertEqual(status, 200)
        data = json.loads(raw)
        self.assertEqual(data["config"]["gateway_profile"], "full")
        saved = json.loads((self.workspace / "workspace_config.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["existing_draft"], "drafts/demo.tex")
        self.assertEqual(saved["active_source_draft_id"], "draft_1")

    def test_import_source_draft_via_api(self) -> None:
        body, boundary = _multipart_body(
            [
                ("files", "source.tex", b"\\documentclass{article}\\begin{document}x\\end{document}"),
                ("files", "preview.pdf", b"%PDF-1.4 test"),
            ]
        )
        status, raw = self._request(
            "POST",
            "/api/workspace/paper_writer/demo/drafts/import",
            body,
            {"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        self.assertEqual(status, 201)
        data = json.loads(raw)
        self.assertIn("draft", data)
        draft_id = data["draft"]["draft_id"]
        self.assertTrue((self.workspace / "source_drafts" / draft_id / "source.tex").exists())

    def test_import_template_directory_via_api(self) -> None:
        body, boundary = _multipart_body(
            [
                ("files", "MyCjC/CjC.cls", b"class"),
                ("files", "MyCjC/CjC_template_tex.tex", b"\\documentclass{CjC}\\begin{document}\\end{document}"),
            ]
        )
        status, raw = self._request(
            "POST",
            "/api/workspace/paper_writer/demo/templates/import",
            body,
            {"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        self.assertEqual(status, 201)
        data = json.loads(raw)
        self.assertEqual(data["template"]["profile"], "cjc")

    def test_post_migrations_delegates_to_process_manager(self) -> None:
        fake_payload = {
            "run_id": "run_test",
            "entry": "migration",
            "workspace_rel": "paper_writer/demo",
            "command": ["python", "-m", "paperforge", "publish"],
            "pid": 123,
            "pgid": 123,
            "status": "running",
            "started_at": "2026-04-02T12:00:00",
            "ended_at": None,
            "log_path": "/tmp/run.log",
            "exit_code": None,
            "details": {},
        }
        with patch.object(server_mod, "PROCESS_MANAGER", Mock(start_run=Mock(return_value=fake_payload))):
            status, raw = self._request_json(
                "POST",
                "/api/workspace/paper_writer/demo/migrations",
                {"source_draft_id": "draft_1", "template_id": "tpl_1", "output_name": "demo"},
            )
        self.assertEqual(status, 201)
        data = json.loads(raw)
        self.assertEqual(data["run_id"], "run_test")

    def test_get_history_returns_workspace_history(self) -> None:
        status, raw = self._request("GET", "/api/workspace/paper_writer/demo/history")
        self.assertEqual(status, 200)
        data = json.loads(raw)
        self.assertIn("history", data)
        self.assertIn("source_drafts", data["history"])

    def test_recycle_and_restore_round_trip_via_api(self) -> None:
        body, boundary = _multipart_body(
            [("files", "source.tex", b"\\documentclass{article}\\begin{document}x\\end{document}")]
        )
        status, raw = self._request(
            "POST",
            "/api/workspace/paper_writer/demo/drafts/import",
            body,
            {"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        self.assertEqual(status, 201)
        draft_id = json.loads(raw)["draft"]["draft_id"]

        status, raw = self._request_json(
            "POST",
            f"/api/workspace/paper_writer/demo/artifacts/source_drafts/{draft_id}/recycle",
            {},
        )
        self.assertEqual(status, 200)
        recycle_id = json.loads(raw)["deleted"]["recycle_id"]

        status, raw = self._request_json(
            "POST",
            f"/api/workspace/paper_writer/demo/recycle/{recycle_id}/restore",
            {},
        )
        self.assertEqual(status, 200)
        self.assertIn("restored", json.loads(raw))

    def test_get_workspace_log_delegates_to_process_manager(self) -> None:
        fake_log = {
            "run_id": "run_test",
            "offset": 0,
            "next_offset": 12,
            "text": "hello world\n",
            "status": "running",
        }
        with patch.object(server_mod, "PROCESS_MANAGER", Mock(read_log=Mock(return_value=fake_log))):
            status, raw = self._request("GET", "/api/workspace/paper_writer/demo/log?run_id=run_test&offset=0")
        self.assertEqual(status, 200)
        data = json.loads(raw)
        self.assertEqual(data["text"], "hello world\n")

    def test_workspace_detail_exposes_v3_status_surfaces(self) -> None:
        status, raw = self._request("GET", "/api/workspace/paper_writer/demo")
        self.assertEqual(status, 200)
        data = json.loads(raw)
        for key in (
            "profile",
            "provider_status",
            "approvals",
            "claim_coverage",
            "resume",
            "artifact_previews",
            "release_gate",
        ):
            self.assertIn(key, data)

    def test_mutation_requires_csrf_session_cookie_and_header(self) -> None:
        status, _ = self._request_json(
            "PUT",
            "/api/workspace/paper_writer/demo/config",
            {"gateway_profile": "safe"},
            authenticated=False,
        )
        self.assertEqual(status, 403)

        status, _ = self._request_json(
            "PUT",
            "/api/workspace/paper_writer/demo/config",
            {"gateway_profile": "safe"},
            authenticated=False,
            headers={
                "X-PaperForge-CSRF": self.csrf_token,
                "Cookie": "PaperForge-Session=wrong",
            },
        )
        self.assertEqual(status, 403)

    def test_browser_cannot_override_cli_command(self) -> None:
        status, raw = self._request_json(
            "POST",
            "/api/runs",
            {
                "entry": "run",
                "profile": "full",
                "workspace_rel": "paper_writer/demo",
                "command": ["/bin/sh", "-c", "id"],
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("forbidden", json.loads(raw)["error"])

    def test_workspace_symlink_escape_is_forbidden(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "workflow_state.json").write_text(
            json.dumps({"secret": "must not leak"}), encoding="utf-8"
        )
        (self.results_dir / "escape").symlink_to(
            outside, target_is_directory=True
        )

        status, raw = self._request("GET", "/api/workspace/escape")
        self.assertEqual(status, 403)
        self.assertNotIn(b"must not leak", raw)

    def test_nested_file_symlink_escape_is_not_read_or_served(self) -> None:
        secret = self.root / "secret.txt"
        secret.write_text("must not leak", encoding="utf-8")
        (self.workspace / "notes.txt").unlink()
        (self.workspace / "notes.txt").symlink_to(secret)

        status, raw = self._request("GET", "/api/workspace/paper_writer/demo")
        self.assertEqual(status, 200)
        self.assertNotIn(b"must not leak", raw)

        status, raw = self._request(
            "GET", "/files/results/paper_writer/demo/notes.txt"
        )
        self.assertEqual(status, 403)
        self.assertNotIn(b"must not leak", raw)

        state_secret = self.root / "state-secret.json"
        state_secret.write_text(
            json.dumps({"idea_name": "must not leak"}), encoding="utf-8"
        )
        (self.workspace / "workflow_state.json").unlink()
        (self.workspace / "workflow_state.json").symlink_to(state_secret)
        status, raw = self._request("GET", "/api/workspaces")
        self.assertEqual(status, 200)
        self.assertNotIn(b"must not leak", raw)

    def test_invalid_host_and_origin_are_forbidden(self) -> None:
        status, _ = self._request(
            "GET",
            "/api/workspaces",
            headers={"Host": f"evil.example:{self.port}"},
        )
        self.assertEqual(status, 403)

        status, _ = self._request(
            "GET",
            "/api/workspaces",
            headers={"Origin": "https://evil.example"},
        )
        self.assertEqual(status, 403)

    def test_v3_database_approvals_resume_artifacts_and_untrusted_release_gate(
        self,
    ) -> None:
        from paperforge.api import PaperForgeService
        from paperforge.experiments import ExperimentManager

        service = PaperForgeService(self.workspace)
        handle = service.run(profile="writing-only")
        proposal = ExperimentManager(
            self.workspace,
            profile="full",
            memory=service.memory,
        ).propose(title="Frontend approval fixture")
        service.approve(proposal.proposal_id)
        (self.workspace / "paper.pdf").write_bytes(b"%PDF-1.4")
        gate = {
            "claim_gate_passed": True,
            "required_artifacts_present": True,
            "latex_clean_compile": True,
            "all_pdf_pages_inspected": True,
            "protected_hashes_unchanged": True,
            "secret_scan_clean": True,
            "release_manifest_verified": True,
        }
        (self.workspace / ".paperforge" / "release-gate.json").write_text(
            json.dumps(gate), encoding="utf-8"
        )

        status, raw = self._request("GET", "/api/workspace/paper_writer/demo")
        self.assertEqual(status, 200)
        data = json.loads(raw)
        self.assertEqual(data["profile"], "writing-only")
        self.assertEqual(data["resume"]["workflow_id"], handle.run_id)
        self.assertTrue(data["resume"]["available"])
        self.assertEqual(
            data["approvals"][0]["proposal_id"],
            proposal.proposal_id,
        )
        self.assertFalse(data["release_gate"]["passed"])
        self.assertEqual(data["release_gate"]["source"], "frontend-derived")
        self.assertIn(
            "paper.pdf", [item["path"] for item in data["artifact_previews"]]
        )
