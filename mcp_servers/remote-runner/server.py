from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

from mcp_servers.base import BasePaperForgeMcpServer, ResourceSpec, ToolSpec


ROOT = Path(__file__).resolve().parents[2]


class RemoteRunnerMcpServer(BasePaperForgeMcpServer):
    server_name = "remote-runner"

    def tools(self) -> List[ToolSpec]:
        return [
            ToolSpec(name="plan_cloud_cycle", description="Build the existing cloud-cycle command line."),
            ToolSpec(name="sync_cloud_results", description="Run or plan sync_cloud_results_to_uploads.py.", requires_workspace_lock=True),
        ]

    def resources(self) -> List[ResourceSpec]:
        return [
            ResourceSpec(
                uri="remote-runner://entrypoints",
                description="Paths to the existing remote execution entrypoints.",
            )
        ]

    def plan_cloud_cycle(
        self,
        workspace: str,
        remote_config: str | None = None,
        cloud_run_dir: str | None = None,
        pipeline_root: str | None = None,
    ) -> Dict[str, Any]:
        command = [sys.executable, str(ROOT / "run_cloud_pipeline_cycle.py"), "--workspace", str(Path(workspace).expanduser().resolve())]
        if remote_config:
            command += ["--remote-config", remote_config]
        if cloud_run_dir:
            command += ["--cloud-run-dir", cloud_run_dir]
        if pipeline_root:
            command += ["--pipeline-root", pipeline_root]
        return {
            "workspace": str(Path(workspace).expanduser().resolve()),
            "command": command,
            "status": "ok",
        }

    def sync_cloud_results(
        self,
        workspace: str,
        cloud_run_dir: str,
        pipeline_config: str | None = None,
        execute: bool = False,
    ) -> Dict[str, Any]:
        workspace_path = Path(workspace).expanduser().resolve()
        command = [
            sys.executable,
            str(ROOT / "sync_cloud_results_to_uploads.py"),
            "--workspace",
            str(workspace_path),
            "--cloud-run-dir",
            str(Path(cloud_run_dir).expanduser().resolve()),
        ]
        if pipeline_config:
            command += ["--pipeline-config", pipeline_config]
        if not execute:
            return {
                "workspace": str(workspace_path),
                "command": command,
                "status": "planned",
            }
        with self.workspace_lock(workspace_path):
            proc = subprocess.run(command, cwd=str(ROOT), text=True, capture_output=True, check=False)
        return {
            "workspace": str(workspace_path),
            "command": command,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "status": "completed" if proc.returncode == 0 else "failed",
        }

    def read_resource(self, resource_uri: str, **payload: Any) -> Dict[str, Any]:
        if resource_uri != "remote-runner://entrypoints":
            return super().read_resource(resource_uri, **payload)
        return {
            "server": self.server_name,
            "resource": resource_uri,
            "entrypoints": {
                "cloud_cycle": str(ROOT / "run_cloud_pipeline_cycle.py"),
                "sync": str(ROOT / "sync_cloud_results_to_uploads.py"),
            },
            "status": "ok",
        }


def build_server() -> RemoteRunnerMcpServer:
    return RemoteRunnerMcpServer()
