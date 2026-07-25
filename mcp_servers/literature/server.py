from __future__ import annotations

import re
from typing import Any

from mcp_servers.base import BasePaperForgeMcpServer, ResourceSpec, ToolSpec


def _normalize_text(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


class LiteratureMcpServer(BasePaperForgeMcpServer):
    server_name = "literature"

    def tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(name="search_papers", description="Search papers via the existing PaperForge literature engine."),
            ToolSpec(name="fetch_bibtex", description="Convert paper identifiers or paper payloads to minimal BibTeX."),
            ToolSpec(name="deduplicate_entries", description="Deduplicate raw literature entries by DOI or normalized title."),
        ]

    def resources(self) -> list[ResourceSpec]:
        return [
            ResourceSpec(
                uri="literature://capabilities",
                description="Static description of the literature MCP bridge and supported engines.",
            )
        ]

    def search_papers(self, query: str, engine: str = "openalex", limit: int = 10) -> dict[str, Any]:
        from engine.generate_ideas import search_for_papers

        try:
            results = search_for_papers(query=query, result_limit=int(limit), engine=engine) or []
            return {
                "request": {
                    "query": query,
                    "engine": engine,
                    "limit": int(limit),
                },
                "results": results,
                "status": "ok",
            }
        except Exception as exc:
            return {
                "request": {
                    "query": query,
                    "engine": engine,
                    "limit": int(limit),
                },
                "results": [],
                "status": "error",
                "error": str(exc),
            }

    def fetch_bibtex(self, identifiers: list[Any]) -> dict[str, Any]:
        entries: list[str] = []
        for index, item in enumerate(identifiers):
            if isinstance(item, dict):
                bibtex = item.get("citationStyles", {}).get("bibtex")
                if isinstance(bibtex, str) and bibtex.strip():
                    entries.append(bibtex.strip())
                    continue
                title = str(item.get("title") or item.get("Title") or f"paper_{index+1}")
                year = str(item.get("year") or item.get("publication_year") or "unknown")
                key = _normalize_text(title)[:24] or f"paper_{index+1}"
            else:
                title = str(item).strip() or f"paper_{index+1}"
                year = "unknown"
                key = _normalize_text(title)[:24] or f"paper_{index+1}"
            entries.append(
                f"@misc{{{key},\n"
                "  title = {" + title + "},\n"
                "  year = {" + year + "}\n"
                "}"
            )
        return {
            "identifiers": identifiers,
            "entries": entries,
            "status": "ok",
        }

    def deduplicate_entries(self, entries: list[dict[str, Any]]) -> dict[str, Any]:
        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for entry in entries:
            doi = str(entry.get("doi") or "").strip().lower()
            title = _normalize_text(str(entry.get("title") or entry.get("Title") or ""))
            year = str(entry.get("year") or entry.get("publication_year") or "").strip()
            fingerprint = f"doi:{doi}" if doi else f"title:{title}|year:{year}"
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            deduped.append(entry)
        return {
            "before": len(entries),
            "after": len(deduped),
            "results": deduped,
            "status": "ok",
        }

    def read_resource(self, resource_uri: str, **payload: Any) -> dict[str, Any]:
        if resource_uri != "literature://capabilities":
            return super().read_resource(resource_uri, **payload)
        return {
            "server": self.server_name,
            "resource": resource_uri,
            "engines": ["openalex", "semanticscholar"],
            "status": "ok",
        }


def build_server() -> LiteratureMcpServer:
    return LiteratureMcpServer()
