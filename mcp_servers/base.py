from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from engine.run_lock import run_lock


@dataclass
class ToolSpec:
    name: str
    description: str
    requires_workspace_lock: bool = False


@dataclass
class ResourceSpec:
    uri: str
    description: str


class BasePaperForgeMcpServer:
    server_name = "base"

    def tools(self) -> list[ToolSpec]:
        return []

    def resources(self) -> list[ResourceSpec]:
        return []

    def capabilities(self) -> dict[str, Any]:
        return {
            "server": self.server_name,
            "tools": [asdict(spec) for spec in self.tools()],
            "resources": [asdict(spec) for spec in self.resources()],
        }

    def call_tool(self, tool_name: str, **payload: Any) -> dict[str, Any]:
        tool = getattr(self, tool_name, None)
        if tool is None or not callable(tool):
            raise ValueError(f"Unsupported tool `{tool_name}` for server `{self.server_name}`")
        result = tool(**payload)
        if isinstance(result, dict):
            return {
                "server": self.server_name,
                "tool": tool_name,
                **result,
            }
        return {
            "server": self.server_name,
            "tool": tool_name,
            "result": result,
        }

    def read_resource(self, resource_uri: str, **payload: Any) -> dict[str, Any]:
        raise ValueError(f"Unsupported resource `{resource_uri}` for server `{self.server_name}`")

    @contextmanager
    def workspace_lock(self, workspace: str | Path) -> Iterator[None]:
        workspace_path = Path(workspace).expanduser().resolve()
        with run_lock(workspace_path, timeout=30, poll_interval=0.2, verbose=True):
            yield
