from __future__ import annotations

import re
from collections import Counter
from typing import Any

from mcp_servers.base import BasePaperForgeMcpServer, ResourceSpec, ToolSpec


def _tokens(text: str) -> list[str]:
    return re.findall(r"[A-Za-z][A-Za-z0-9_'-]*", text.lower())


class AigcEvalMcpServer(BasePaperForgeMcpServer):
    server_name = "aigc-eval"

    def tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(name="inspect_text", description="Inspect repeated phrasing and basic length statistics."),
            ToolSpec(name="compare_texts", description="Estimate token-overlap similarity between two texts."),
        ]

    def resources(self) -> list[ResourceSpec]:
        return [
            ResourceSpec(
                uri="aigc-eval://guidance",
                description="Static interpretation guide for the lightweight AIGC heuristics.",
            )
        ]

    def inspect_text(self, text: str) -> dict[str, Any]:
        tokens = _tokens(text)
        phrases = [" ".join(tokens[index : index + 3]) for index in range(max(0, len(tokens) - 2))]
        repeated = [
            {"phrase": phrase, "count": count}
            for phrase, count in Counter(phrases).most_common(5)
            if count > 1
        ]
        avg_sentence_length = 0.0
        sentences = [segment.strip() for segment in re.split(r"[.!?]+", text) if segment.strip()]
        if sentences:
            avg_sentence_length = round(sum(len(_tokens(sentence)) for sentence in sentences) / len(sentences), 2)
        risk = "low"
        if repeated or avg_sentence_length > 28:
            risk = "medium"
        if len(repeated) >= 3 or avg_sentence_length > 36:
            risk = "high"
        return {
            "token_count": len(tokens),
            "sentence_count": len(sentences),
            "avg_sentence_length": avg_sentence_length,
            "repeated_phrases": repeated,
            "risk": risk,
            "status": "ok",
        }

    def compare_texts(self, source: str, rewritten: str) -> dict[str, Any]:
        left = set(_tokens(source))
        right = set(_tokens(rewritten))
        if not left and not right:
            score = 1.0
        elif not left or not right:
            score = 0.0
        else:
            score = round(len(left & right) / len(left | right), 4)
        return {
            "similarity": score,
            "status": "ok",
        }

    def read_resource(self, resource_uri: str, **payload: Any) -> dict[str, Any]:
        if resource_uri != "aigc-eval://guidance":
            return super().read_resource(resource_uri, **payload)
        return {
            "server": self.server_name,
            "resource": resource_uri,
            "guidance": {
                "low": "Few repeated phrases and compact sentence structure.",
                "medium": "Some repetitive wording or long sentences; manual review recommended.",
                "high": "Repeated phrasing or overly long sentences suggest a rewrite pass.",
            },
            "status": "ok",
        }


def build_server() -> AigcEvalMcpServer:
    return AigcEvalMcpServer()
