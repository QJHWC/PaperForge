"""针对 frontend/server.py 路径穿越与 CORS 修复的安全回归测试。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from frontend.server import Handler, RESULTS_DIR, FRONTEND_DIR, ROOT as SERVER_ROOT
from http.server import HTTPServer


def _start_server(host: str = "127.0.0.1", port: int = 0) -> tuple[HTTPServer, int]:
    """启动一个绑定随机端口的测试服务器，返回 (server, port)。"""
    server = HTTPServer((host, port), Handler)
    actual_port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, actual_port


class TestPathTraversalWorkspace(unittest.TestCase):
    """验证 /api/workspace/ 路径穿越防护。"""

    @classmethod
    def setUpClass(cls):
        cls.server, cls.port = _start_server()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def _get(self, path: str):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status, body

    def test_path_traversal_dotdot_blocked(self):
        """../../etc/passwd 形式的穿越请求应返回 403。"""
        status, _ = self._get("/api/workspace/../../etc")
        self.assertEqual(status, 403)

    def test_absolute_path_outside_results_blocked(self):
        """URL 编码的 %2Ftmp 被 urlparse 保留为字面量，拼接在 RESULTS_DIR 下，
        不存在则返回 404（而非意外访问真实 /tmp），说明路径边界未被突破。"""
        status, _ = self._get("/api/workspace/%2Ftmp")
        # 404 表示路径不存在于 results/ 内，403 也可接受——重要的是不返回 200
        self.assertIn(status, (403, 404))

    def test_valid_nonexistent_workspace_returns_404(self):
        """results/ 内不存在的路径应返回 404，而非 403 或 500。"""
        status, body = self._get("/api/workspace/nonexistent_experiment/run_1")
        self.assertEqual(status, 404)
        data = json.loads(body)
        self.assertIn("error", data)


class TestPathTraversalStaticFiles(unittest.TestCase):
    """验证静态文件处理器路径穿越防护。"""

    @classmethod
    def setUpClass(cls):
        cls.server, cls.port = _start_server()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def _get(self, path: str):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status, body

    def test_dotdot_static_file_blocked(self):
        """尝试访问前端目录之外的文件应返回 404（路径不存在于 FRONTEND_DIR 内）。"""
        # 路径解析后超出 FRONTEND_DIR，应被拦截（返回 404）
        status, _ = self._get("/../engine/perform_writeup.py")
        self.assertNotEqual(status, 200)


class TestCORSHeader(unittest.TestCase):
    """验证 CORS 响应头不再使用通配符。"""

    @classmethod
    def setUpClass(cls):
        cls.server, cls.port = _start_server()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_cors_not_wildcard_on_api(self):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/workspaces")
        resp = conn.getresponse()
        resp.read()
        cors = resp.getheader("Access-Control-Allow-Origin", "")
        conn.close()
        self.assertNotEqual(cors, "*", "CORS header must not be wildcard")
        self.assertIn("127.0.0.1", cors)


class TestResultsDirConstant(unittest.TestCase):
    """验证 RESULTS_DIR 指向 ROOT/results 的绝对路径。"""

    def test_results_dir_is_absolute(self):
        self.assertTrue(RESULTS_DIR.is_absolute())

    def test_results_dir_under_root(self):
        self.assertTrue(str(RESULTS_DIR).startswith(str(SERVER_ROOT)))


if __name__ == "__main__":
    unittest.main()
