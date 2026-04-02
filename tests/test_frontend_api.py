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

    def tearDown(self) -> None:
        self.server.shutdown()
        self.patcher_pm.stop()
        self.patcher_root.stop()
        self.patcher_results.stop()
        self.tmp.cleanup()

    def _request(self, method: str, path: str, body: bytes | None = None, headers: dict | None = None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return resp.status, raw

    def _request_json(self, method: str, path: str, body: dict | None = None):
        payload = json.dumps(body or {}).encode("utf-8")
        return self._request(method, path, payload, {"Content-Type": "application/json"})

    def test_put_workspace_config_persists_file(self) -> None:
        status, raw = self._request_json(
            "PUT",
            "/api/workspace/paper_writer/demo/config",
            {
                "writeup_model": "gpt-5.4-xhigh",
                "gateway_profile": "full",
                "existing_draft": "drafts/demo.tex",
                "enforce_disclosure": True,
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
            "command": ["python", "launch_template_migration.py"],
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
