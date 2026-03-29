from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List

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

    def tools(self) -> List[ToolSpec]:
        return []

    def resources(self) -> List[ResourceSpec]:
        return []

    def capabilities(self) -> Dict[str, Any]:
        return {
            "server": self.server_name,
            "tools": [asdict(spec) for spec in self.tools()],
            "resources": [asdict(spec) for spec in self.resources()],
        }

    def call_tool(self, tool_name: str, **payload: Any) -> Dict[str, Any]:
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

    def read_resource(self, resource_uri: str, **payload: Any) -> Dict[str, Any]:
        raise ValueError(f"Unsupported resource `{resource_uri}` for server `{self.server_name}`")

    @contextmanager
    def workspace_lock(self, workspace: str | Path) -> Iterator[None]:
        workspace_path = Path(workspace).expanduser().resolve()
        with run_lock(workspace_path, timeout=30, poll_interval=0.2, verbose=True):
            yield
