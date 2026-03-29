from __future__ import annotations

from typing import Any, Dict, List

from engine.bibliography import (
    bibliography_summary,
    deduplicate_library,
    export_bibtex,
    import_bibtex,
    load_workspace_library,
    record_writeup_citations,
    section_entries,
)
from mcp_servers.base import BasePaperForgeMcpServer, ResourceSpec, ToolSpec


class BibliographyMcpServer(BasePaperForgeMcpServer):
    server_name = "bibliography"

    def tools(self) -> List[ToolSpec]:
        return [
            ToolSpec(name="summary", description="Return the current bibliography summary."),
            ToolSpec(name="import_bibtex_entries", description="Import BibTeX into the workspace bibliography store.", requires_workspace_lock=True),
            ToolSpec(name="export_bibtex_entries", description="Export BibTeX from the workspace bibliography store."),
            ToolSpec(name="deduplicate_library_entries", description="Deduplicate the workspace bibliography store.", requires_workspace_lock=True),
            ToolSpec(name="map_section_citations", description="Map citation keys to a writeup section.", requires_workspace_lock=True),
        ]

    def resources(self) -> List[ResourceSpec]:
        return [
            ResourceSpec(
                uri="bibliography://workspace-library",
                description="Serialized bibliography state for a workspace.",
            )
        ]

    def summary(self, workspace: str) -> Dict[str, Any]:
        return {
            "summary": bibliography_summary(workspace),
            "status": "ok",
        }

    def import_bibtex_entries(self, workspace: str, bibtex_text: str, source: str = "manual") -> Dict[str, Any]:
        result = import_bibtex(workspace, bibtex_text, source=source, acquire_lock=True)
        result["status"] = "ok"
        return result

    def export_bibtex_entries(self, workspace: str, entry_ids: List[str] | None = None) -> Dict[str, Any]:
        return {
            "workspace": workspace,
            "bibtex": export_bibtex(workspace, entry_ids=entry_ids),
            "status": "ok",
        }

    def deduplicate_library_entries(self, workspace: str) -> Dict[str, Any]:
        result = deduplicate_library(workspace, acquire_lock=True)
        result["status"] = "ok"
        return result

    def map_section_citations(self, workspace: str, section_name: str, citation_keys: List[str]) -> Dict[str, Any]:
        result = record_writeup_citations(
            workspace=workspace,
            section_name=section_name,
            citation_keys=citation_keys,
            acquire_lock=True,
        )
        result["section_entries"] = section_entries(workspace, section_name)
        result["status"] = "ok"
        return result

    def read_resource(self, resource_uri: str, **payload: Any) -> Dict[str, Any]:
        if resource_uri != "bibliography://workspace-library":
            return super().read_resource(resource_uri, **payload)
        workspace = str(payload["workspace"])
        return {
            "server": self.server_name,
            "resource": resource_uri,
            "workspace": workspace,
            "library": load_workspace_library(workspace),
            "status": "ok",
        }


def build_server() -> BibliographyMcpServer:
    return BibliographyMcpServer()
