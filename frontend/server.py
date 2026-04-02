"""PaperForge 前端 HTTP 服务器。

提供静态文件服务 + JSON API，供浏览器前端查询 workspace、控制流程、读取日志与打开产物。
"""

from __future__ import annotations

import argparse
import cgi
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = Path(__file__).resolve().parent
RESULTS_DIR = (ROOT / "results").resolve()

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import json as _json
import re as _re

from engine.workspace_config import load_workspace_config, save_workspace_config
from engine.template_migration import (
    import_source_draft,
    import_template_directory,
    load_workspace_history,
    recycle_workspace_artifact,
    refresh_workspace_history,
    restore_recycled_artifact,
)
from frontend.process_manager import ProcessManager

PROCESS_MANAGER = ProcessManager(root=ROOT, results_dir=RESULTS_DIR)
_RUN_DIR_PATTERN = _re.compile(r"^run_(\d+)$")
_STATE_SUMMARY_KEYS = ["phase", "current_phase", "completed_phases", "status", "idea_name", "created_at", "updated_at"]
_CONFIG_KEYS = {
    "writeup_model",
    "gateway_profile",
    "existing_draft",
    "enforce_disclosure",
    "skip_chktex_fix",
    "active_source_draft_id",
    "active_template_id",
    "latest_migration_id",
}


def _workspace_summary(run_dir: Path) -> dict:
    state_path = run_dir / "workflow_state.json"
    state_summary = {}
    if state_path.exists():
        try:
            raw = _json.loads(state_path.read_text(encoding="utf-8"))
            state_summary = {k: raw.get(k) for k in _STATE_SUMMARY_KEYS if k in raw}
        except Exception as exc:
            state_summary = {"parse_error": str(exc)}
    run_count = sum(1 for d in run_dir.iterdir() if d.is_dir() and _RUN_DIR_PATTERN.match(d.name))
    pdfs = sorted([f.name for f in run_dir.glob("*.pdf")])
    frontend_run_dir = run_dir / "artifacts" / "frontend_runs"
    frontend_runs = len(list(frontend_run_dir.glob("*.json"))) if frontend_run_dir.exists() else 0
    return {
        "workspace": str(run_dir.resolve()),
        "workspace_rel": run_dir.resolve().relative_to(RESULTS_DIR).as_posix(),
        "experiment": run_dir.parent.name,
        "run_name": run_dir.name,
        "state": state_summary,
        "state_exists": state_path.exists(),
        "run_count": run_count,
        "artifacts": {
            "pdfs": pdfs,
            "frontend_runs": frontend_runs,
        },
    }


def list_workspaces(results_root: str):
    root = Path(results_root)
    if not root.is_dir():
        return []
    workspaces = []
    for exp_dir in sorted(root.iterdir()):
        if not exp_dir.is_dir() or exp_dir.name.startswith("."):
            continue
        for run_dir in sorted(exp_dir.iterdir()):
            if not run_dir.is_dir() or run_dir.name.startswith("."):
                continue
            workspaces.append(_workspace_summary(run_dir))
    return workspaces


def _safe_workspace_path(workspace_rel: str) -> Path:
    ws = (RESULTS_DIR / workspace_rel).resolve()
    if not str(ws).startswith(str(RESULTS_DIR) + os.sep) and str(ws) != str(RESULTS_DIR):
        raise PermissionError("forbidden")
    return ws


def _safe_results_file(path_suffix: str) -> Path:
    file_path = (RESULTS_DIR / path_suffix).resolve()
    if not str(file_path).startswith(str(RESULTS_DIR) + os.sep):
        raise PermissionError("forbidden")
    return file_path


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        print(f"[{self.address_string()}] {format % args}")

    def _send_json(self, data: object, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", f"http://127.0.0.1:{self.server.server_address[1]}")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        suffix = path.suffix.lower()
        mime = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".log": "text/plain; charset=utf-8",
            ".txt": "text/plain; charset=utf-8",
            ".tex": "text/plain; charset=utf-8",
            ".pdf": "application/pdf",
        }.get(suffix, "application/octet-stream")
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length > 0 else b"{}"
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _read_multipart_files(self) -> list[dict]:
        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                "CONTENT_LENGTH": self.headers.get("Content-Length", "0"),
            },
        )
        uploaded: list[dict] = []
        for item in form.list or []:
            if not getattr(item, "filename", None):
                continue
            uploaded.append(
                {
                    "field": item.name,
                    "filename": str(item.filename),
                    "content": item.file.read(),
                }
            )
        return uploaded

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/api/workspaces":
            self._send_json({"workspaces": list_workspaces(str(RESULTS_DIR)), "results_root": str(RESULTS_DIR)})
            return

        if path.startswith("/api/workspace/") and path.endswith("/log"):
            workspace_rel = unquote(path[len("/api/workspace/") : -len("/log")].strip("/"))
            try:
                _safe_workspace_path(workspace_rel)
            except PermissionError:
                self._send_json({"error": "forbidden"}, 403)
                return
            query = parse_qs(parsed.query)
            run_id = (query.get("run_id") or [""])[0]
            offset = int((query.get("offset") or ["0"])[0] or 0)
            try:
                payload = PROCESS_MANAGER.read_log(workspace_rel, run_id, offset=offset)
            except FileNotFoundError:
                self._send_json({"error": "run not found"}, 404)
                return
            self._send_json(payload)
            return

        if path.startswith("/api/workspace/") and path.endswith("/history"):
            workspace_rel = unquote(path[len("/api/workspace/") : -len("/history")].strip("/"))
            try:
                ws = _safe_workspace_path(workspace_rel)
            except PermissionError:
                self._send_json({"error": "forbidden"}, 403)
                return
            if not ws.is_dir():
                self._send_json({"error": f"workspace not found: {workspace_rel}"}, 404)
                return
            history = refresh_workspace_history(ws)
            self._send_json({"workspace_rel": workspace_rel, "history": history})
            return

        if path.startswith("/api/workspace/"):
            workspace_rel = unquote(path[len("/api/workspace/") :].strip("/"))
            try:
                ws = _safe_workspace_path(workspace_rel)
            except PermissionError:
                self._send_json({"error": "forbidden"}, 403)
                return
            if not ws.is_dir():
                self._send_json({"error": f"workspace not found: {workspace_rel}"}, 404)
                return

            state_file = ws / "workflow_state.json"
            state = {}
            if state_file.exists():
                try:
                    state = json.loads(state_file.read_text(encoding="utf-8"))
                except Exception:
                    pass
            notes_file = ws / "notes.txt"
            notes = notes_file.read_text(encoding="utf-8")[:4000] if notes_file.exists() else ""
            history = refresh_workspace_history(ws)
            run_dirs = sorted(
                [d for d in ws.iterdir() if d.is_dir() and _RUN_DIR_PATTERN.match(d.name)],
                key=lambda d: int(_RUN_DIR_PATTERN.match(d.name).group(1)),
            )
            runs = []
            for rd in run_dirs:
                fi = rd / "final_info.json"
                run_data = {"name": rd.name, "has_result": fi.exists()}
                if fi.exists():
                    try:
                        run_data["keys"] = list(json.loads(fi.read_text(encoding="utf-8")).keys())
                    except Exception:
                        pass
                runs.append(run_data)
            latex_dir = ws / "latex"
            latex = {}
            if latex_dir.is_dir():
                latex = {
                    "tex_files": [f.name for f in latex_dir.glob("*.tex")],
                    "pdf_files": [f.name for f in latex_dir.glob("*.pdf")],
                    "log_files": [f.name for f in latex_dir.glob("*.log")],
                }
            self._send_json(
                {
                    "workspace": str(ws),
                    "workspace_rel": workspace_rel,
                    "state": state,
                    "config": load_workspace_config(ws),
                    "notes_preview": notes,
                    "history": history,
                    "source_drafts": history.get("source_drafts", []),
                    "templates": history.get("templates", []),
                    "migrations": history.get("migrations", []),
                    "deleted_items": history.get("deleted_items", []),
                    "runs": runs,
                    "latex": latex,
                    "root_pdf_files": [f.name for f in ws.glob("*.pdf")],
                    "frontend_runs": PROCESS_MANAGER.list_run_records(workspace_rel),
                }
            )
            return

        if path.startswith("/files/results/"):
            rel = unquote(path[len("/files/results/") :].lstrip("/"))
            try:
                file_path = _safe_results_file(rel)
            except PermissionError:
                self.send_response(403)
                self.end_headers()
                return
            if not file_path.is_file():
                self.send_response(404)
                self.end_headers()
                return
            self._send_file(file_path)
            return

        if path == "/":
            self._send_file(FRONTEND_DIR / "index.html")
            return

        file_path = (FRONTEND_DIR / path.lstrip("/")).resolve()
        if str(file_path).startswith(str(FRONTEND_DIR) + os.sep) and file_path.is_file():
            self._send_file(file_path)
            return

        self.send_response(404)
        self.end_headers()

    def do_PUT(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if not path.startswith("/api/workspace/") or not path.endswith("/config"):
            self.send_response(404)
            self.end_headers()
            return
        workspace_rel = unquote(path[len("/api/workspace/") : -len("/config")].strip("/"))
        try:
            ws = _safe_workspace_path(workspace_rel)
        except PermissionError:
            self._send_json({"error": "forbidden"}, 403)
            return
        if not ws.is_dir():
            self._send_json({"error": "workspace not found"}, 404)
            return
        payload = self._read_json_body()
        merged = load_workspace_config(ws)
        for key, value in payload.items():
            if key in _CONFIG_KEYS:
                merged[key] = value
        saved = save_workspace_config(ws, merged)
        self._send_json({"workspace_rel": workspace_rel, "config": saved})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path.startswith("/api/workspace/") and path.endswith("/drafts/import"):
            workspace_rel = unquote(path[len("/api/workspace/") : -len("/drafts/import")].strip("/"))
            try:
                ws = _safe_workspace_path(workspace_rel)
            except PermissionError:
                self._send_json({"error": "forbidden"}, 403)
                return
            if not ws.is_dir():
                self._send_json({"error": "workspace not found"}, 404)
                return
            try:
                uploaded = self._read_multipart_files()
                manifest = import_source_draft(ws, uploaded)
            except Exception as exc:
                self._send_json({"error": str(exc)}, 400)
                return
            self._send_json({"workspace_rel": workspace_rel, "draft": manifest, "history": load_workspace_history(ws)}, 201)
            return

        if path.startswith("/api/workspace/") and path.endswith("/templates/import"):
            workspace_rel = unquote(path[len("/api/workspace/") : -len("/templates/import")].strip("/"))
            try:
                ws = _safe_workspace_path(workspace_rel)
            except PermissionError:
                self._send_json({"error": "forbidden"}, 403)
                return
            if not ws.is_dir():
                self._send_json({"error": "workspace not found"}, 404)
                return
            try:
                uploaded = self._read_multipart_files()
                manifest = import_template_directory(ws, uploaded)
            except Exception as exc:
                self._send_json({"error": str(exc)}, 400)
                return
            self._send_json({"workspace_rel": workspace_rel, "template": manifest, "history": load_workspace_history(ws)}, 201)
            return

        if path.startswith("/api/workspace/") and path.endswith("/migrations"):
            workspace_rel = unquote(path[len("/api/workspace/") : -len("/migrations")].strip("/"))
            try:
                ws = _safe_workspace_path(workspace_rel)
            except PermissionError:
                self._send_json({"error": "forbidden"}, 403)
                return
            if not ws.is_dir():
                self._send_json({"error": "workspace not found"}, 404)
                return
            payload = self._read_json_body()
            try:
                result = PROCESS_MANAGER.start_run(
                    {
                        "entry": "migration",
                        "workspace_rel": workspace_rel,
                        "source_draft_id": payload.get("source_draft_id"),
                        "template_id": payload.get("template_id"),
                        "output_name": payload.get("output_name"),
                    }
                )
            except Exception as exc:
                self._send_json({"error": str(exc)}, 400)
                return
            self._send_json(result, 201)
            return

        if path.startswith("/api/workspace/") and "/artifacts/" in path and path.endswith("/recycle"):
            prefix = "/api/workspace/"
            workspace_and_rest = unquote(path[len(prefix) :].strip("/"))
            workspace_rel, _, rest = workspace_and_rest.partition("/artifacts/")
            kind, _, tail = rest.partition("/")
            item_id = tail[: -len("/recycle")] if tail.endswith("/recycle") else ""
            try:
                ws = _safe_workspace_path(workspace_rel)
            except PermissionError:
                self._send_json({"error": "forbidden"}, 403)
                return
            try:
                result = recycle_workspace_artifact(ws, kind, item_id)
            except FileNotFoundError:
                self._send_json({"error": "artifact not found"}, 404)
                return
            except Exception as exc:
                self._send_json({"error": str(exc)}, 400)
                return
            self._send_json({"workspace_rel": workspace_rel, "deleted": result, "history": load_workspace_history(ws)})
            return

        if path.startswith("/api/workspace/") and "/recycle/" in path and path.endswith("/restore"):
            prefix = "/api/workspace/"
            workspace_and_rest = unquote(path[len(prefix) :].strip("/"))
            workspace_rel, _, rest = workspace_and_rest.partition("/recycle/")
            recycle_id = rest[: -len("/restore")] if rest.endswith("/restore") else ""
            try:
                ws = _safe_workspace_path(workspace_rel)
            except PermissionError:
                self._send_json({"error": "forbidden"}, 403)
                return
            try:
                result = restore_recycled_artifact(ws, recycle_id)
            except FileNotFoundError:
                self._send_json({"error": "recycle item not found"}, 404)
                return
            except Exception as exc:
                self._send_json({"error": str(exc)}, 400)
                return
            self._send_json({"workspace_rel": workspace_rel, "restored": result, "history": load_workspace_history(ws)})
            return

        payload = self._read_json_body()

        if path == "/api/runs":
            try:
                result = PROCESS_MANAGER.start_run(payload)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, 400)
                return
            self._send_json(result, 201)
            return

        if path.startswith("/api/runs/") and path.endswith("/action"):
            run_id = unquote(path[len("/api/runs/") : -len("/action")].strip("/"))
            action = str(payload.get("action") or "").strip().lower()
            try:
                if action == "pause":
                    result = PROCESS_MANAGER.pause_run(run_id)
                elif action == "resume":
                    result = PROCESS_MANAGER.resume_run(run_id)
                elif action == "stop":
                    result = PROCESS_MANAGER.stop_run(run_id)
                else:
                    self._send_json({"error": "unsupported action"}, 400)
                    return
            except KeyError:
                self._send_json({"error": "run not found"}, 404)
                return
            except RuntimeError as exc:
                self._send_json({"error": str(exc)}, 400)
                return
            self._send_json(result)
            return

        self.send_response(404)
        self.end_headers()


def main() -> None:
    parser = argparse.ArgumentParser(description="PaperForge 前端服务器")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"PaperForge 工作台已启动: http://{args.host}:{args.port}")
    print("Ctrl+C 退出")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")


if __name__ == "__main__":
    main()
