from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from typing import Any

from mcp_servers.base import BasePaperForgeMcpServer

ROOT = Path(__file__).resolve().parent


def _module_name_for_server(server_name: str) -> str:
    return f"paperforge_mcp_{server_name.replace('-', '_')}"


def load_server(server_name: str) -> BasePaperForgeMcpServer:
    server_path = ROOT / server_name / "server.py"
    if not server_path.exists():
        raise FileNotFoundError(f"MCP server not found: {server_path}")
    spec = importlib.util.spec_from_file_location(_module_name_for_server(server_name), server_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Failed to load MCP server: {server_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    build_server = getattr(module, "build_server", None)
    if callable(build_server):
        server = build_server()
        if isinstance(server, BasePaperForgeMcpServer):
            return server
        raise TypeError(f"build_server() from `{server_name}` did not return BasePaperForgeMcpServer")

    for value in module.__dict__.values():
        if isinstance(value, type) and issubclass(value, BasePaperForgeMcpServer) and value is not BasePaperForgeMcpServer:
            return value()

    raise TypeError(f"No MCP server class found in `{server_path}`")


def invoke_server(
    server_name: str,
    *,
    tool: str | None = None,
    resource: str | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    server = load_server(server_name)
    if tool:
        return server.call_tool(tool, **(payload or {}))
    if resource:
        return server.read_resource(resource, **(payload or {}))
    return server.capabilities()


def main() -> None:
    parser = argparse.ArgumentParser(description="PaperForge MCP server runner")
    parser.add_argument("--server", required=True)
    parser.add_argument("--tool", default=None)
    parser.add_argument("--resource", default=None)
    parser.add_argument("--payload", default="{}")
    args = parser.parse_args()

    payload = json.loads(args.payload)
    result = invoke_server(
        args.server,
        tool=args.tool,
        resource=args.resource,
        payload=payload,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
