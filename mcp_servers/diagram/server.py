from __future__ import annotations

from typing import Any, Dict, List

from mcp_servers.base import BasePaperForgeMcpServer, ResourceSpec, ToolSpec


class DiagramMcpServer(BasePaperForgeMcpServer):
    server_name = "diagram"

    def tools(self) -> List[ToolSpec]:
        return [
            ToolSpec(name="generate_block_diagram", description="Generate a lightweight Mermaid block diagram."),
            ToolSpec(name="suggest_figure_caption", description="Produce short academic figure captions."),
        ]

    def resources(self) -> List[ResourceSpec]:
        return [
            ResourceSpec(
                uri="diagram://templates",
                description="Built-in diagram and caption templates.",
            )
        ]

    def generate_block_diagram(self, title: str, modules: List[str], flows: List[str] | None = None) -> Dict[str, Any]:
        lines = ["flowchart LR"]
        for index, module in enumerate(modules):
            lines.append(f"  n{index}[{module}]")
        for index in range(len(modules) - 1):
            lines.append(f"  n{index} --> n{index + 1}")
        for flow in flows or []:
            lines.append(f"  %% {flow}")
        return {
            "title": title,
            "mermaid": "\n".join(lines),
            "status": "ok",
        }

    def suggest_figure_caption(self, figure_type: str, key_points: List[str]) -> Dict[str, Any]:
        bullet_points = "; ".join(key_points[:4])
        return {
            "caption": f"{figure_type.capitalize()} summarizing {bullet_points}.",
            "status": "ok",
        }

    def read_resource(self, resource_uri: str, **payload: Any) -> Dict[str, Any]:
        if resource_uri != "diagram://templates":
            return super().read_resource(resource_uri, **payload)
        return {
            "server": self.server_name,
            "resource": resource_uri,
            "templates": {
                "block_diagram": "flowchart LR",
                "caption_formula": "<figure type> summarizing <key points>.",
            },
            "status": "ok",
        }


def build_server() -> DiagramMcpServer:
    return DiagramMcpServer()
