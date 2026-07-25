from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp_servers.base import BasePaperForgeMcpServer, ResourceSpec, ToolSpec


def _safe_workspace_path(workspace: str, relative_path: str) -> Path:
    root = Path(workspace).expanduser().resolve()
    target = (root / relative_path).resolve()
    if root != target and root not in target.parents:
        raise ValueError("relative_path escapes workspace")
    return target


class FileGatewayMcpServer(BasePaperForgeMcpServer):
    server_name = "file-gateway"

    def tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(name="snapshot_workspace", description="List workspace files and directories."),
            ToolSpec(name="read_text", description="Read a UTF-8 text file inside a workspace."),
            ToolSpec(name="write_text", description="Write a UTF-8 text file inside a workspace with the workspace lock.", requires_workspace_lock=True),
        ]

    def resources(self) -> list[ResourceSpec]:
        return [
            ResourceSpec(
                uri="file-gateway://workspace-tree",
                description="Directory snapshot of a PaperForge workspace.",
            )
        ]

    def snapshot_workspace(self, workspace: str, max_entries: int = 200) -> dict[str, Any]:
        root = Path(workspace).expanduser().resolve()
        entries: list[dict[str, Any]] = []
        for index, path in enumerate(sorted(root.rglob("*"))):
            if index >= max_entries:
                break
            entries.append(
                {
                    "path": str(path.relative_to(root)),
                    "kind": "directory" if path.is_dir() else "file",
                }
            )
        return {
            "workspace": str(root),
            "entries": entries,
            "status": "ok",
        }

    def read_text(self, workspace: str, relative_path: str) -> dict[str, Any]:
        target = _safe_workspace_path(workspace, relative_path)
        return {
            "workspace": str(Path(workspace).expanduser().resolve()),
            "path": str(target),
            "content": target.read_text(encoding="utf-8"),
            "status": "ok",
        }

    def write_text(self, workspace: str, relative_path: str, content: str) -> dict[str, Any]:
        root = Path(workspace).expanduser().resolve()
        target = _safe_workspace_path(workspace, relative_path)
        with self.workspace_lock(root):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return {
            "workspace": str(root),
            "path": str(target),
            "bytes_written": len(content.encode("utf-8")),
            "status": "ok",
        }

    def read_resource(self, resource_uri: str, **payload: Any) -> dict[str, Any]:
        if resource_uri != "file-gateway://workspace-tree":
            return super().read_resource(resource_uri, **payload)
        return self.snapshot_workspace(workspace=str(payload["workspace"]), max_entries=int(payload.get("max_entries", 200)))


def build_server() -> FileGatewayMcpServer:
    return FileGatewayMcpServer()
