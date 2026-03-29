"""PaperForge 前端 HTTP 服务器。

提供静态文件服务 + JSON API，供前端实时查询工作区状态。

用法:
    python frontend/server.py              # 默认 8080 端口
    python frontend/server.py --port 9090  # 自定义端口
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = Path(__file__).resolve().parent
RESULTS_DIR = (ROOT / "results").resolve()

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 直接内联 list_workspaces 逻辑，避免 module 路径问题
import re as _re
import json as _json

_RUN_DIR_PATTERN = _re.compile(r"^run_(\d+)$")
_STATE_SUMMARY_KEYS = ["phase", "current_phase", "completed_phases", "status", "idea_name", "created_at", "updated_at"]

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
            state_path = run_dir / "workflow_state.json"
            state_summary = {}
            if state_path.exists():
                try:
                    raw = _json.loads(state_path.read_text(encoding="utf-8"))
                    state_summary = {k: raw.get(k) for k in _STATE_SUMMARY_KEYS if k in raw}
                except Exception as e:
                    state_summary = {"parse_error": str(e)}
            run_count = sum(1 for d in run_dir.iterdir() if d.is_dir() and _RUN_DIR_PATTERN.match(d.name))
            workspaces.append({
                "workspace": str(run_dir.resolve()),
                "experiment": exp_dir.name,
                "run_name": run_dir.name,
                "state": state_summary,
                "state_exists": state_path.exists(),
                "run_count": run_count,
            })
    return workspaces


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
        }.get(suffix, "application/octet-stream")
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        # ── API 路由 ──────────────────────────────────────────────
        if path == "/api/workspaces":
            results_root = str(ROOT / "results")
            workspaces = list_workspaces(results_root)
            self._send_json({"workspaces": workspaces, "results_root": results_root})
            return

        if path.startswith("/api/workspace/"):
            ws_path = path[len("/api/workspace/"):]
            ws = (RESULTS_DIR / ws_path).resolve()
            # 防止路径穿越：只允许访问 results/ 目录内的路径
            if not str(ws).startswith(str(RESULTS_DIR) + os.sep) and str(ws) != str(RESULTS_DIR):
                self._send_json({"error": "forbidden"}, 403)
                return
            if not ws.is_dir():
                self._send_json({"error": f"workspace not found: {ws_path}"}, 404)
                return
            state_file = ws / "workflow_state.json"
            state = {}
            if state_file.exists():
                try:
                    state = json.loads(state_file.read_text(encoding="utf-8"))
                except Exception:
                    pass
            notes_file = ws / "notes.txt"
            notes = ""
            if notes_file.exists():
                try:
                    notes = notes_file.read_text(encoding="utf-8")[:2000]
                except Exception:
                    pass
            import re
            run_dirs = sorted(
                [d for d in ws.iterdir() if d.is_dir() and re.match(r"run_\d+$", d.name)],
                key=lambda d: int(re.match(r"run_(\d+)$", d.name).group(1)),
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
                }
            self._send_json({
                "workspace": str(ws),
                "state": state,
                "notes_preview": notes,
                "runs": runs,
                "latex": latex,
            })
            return

        # ── 静态文件 ──────────────────────────────────────────────
        if path == "/":
            self._send_file(FRONTEND_DIR / "index.html")
            return
        file_path = (FRONTEND_DIR / path.lstrip("/")).resolve()
        # 防止路径穿越：只允许访问前端目录内的文件
        if str(file_path).startswith(str(FRONTEND_DIR) + os.sep) and file_path.is_file():
            self._send_file(file_path)
            return

        self.send_response(404)
        self.end_headers()


def main() -> None:
    parser = argparse.ArgumentParser(description="PaperForge 前端服务器")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    server = HTTPServer((args.host, args.port), Handler)
    print(f"PaperForge 工作台已启动: http://{args.host}:{args.port}")
    print("Ctrl+C 退出")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")


if __name__ == "__main__":
    main()
