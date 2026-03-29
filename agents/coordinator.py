from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .mvp_workflow_agent import MvpWorkflowAgent
from .runtime import load_schema
from .scientist_workflow_agent import ScientistWorkflowAgent
from .writeup_agent import WriteupAgent


@dataclass
class CoordinatorResult:
    mode: str
    payload: Dict[str, Any] = field(default_factory=dict)


class PaperForgeCoordinator:
    """Top-level router for the bridged agent layer."""

    schema_name = "coordinator"

    def __init__(self) -> None:
        self.scientist = ScientistWorkflowAgent()
        self.mvp = MvpWorkflowAgent()
        self.writeup = WriteupAgent()

    def input_schema(self) -> Dict[str, Any]:
        return load_schema(self.schema_name)

    def run(self, mode: str, **kwargs: Any) -> CoordinatorResult:
        normalized_mode = (mode or "").strip().lower()
        if normalized_mode == "scientist":
            return CoordinatorResult(mode="scientist", payload=self.scientist.run(**kwargs))
        if normalized_mode == "mvp":
            return CoordinatorResult(mode="mvp", payload=self.mvp.run(**kwargs))
        if normalized_mode == "writeup":
            return CoordinatorResult(mode="writeup", payload=self.writeup.run(**kwargs))
        raise ValueError(f"Unsupported mode: {mode}")

    def status_snapshot(self) -> Dict[str, Any]:
        return {
            "modes": ["scientist", "mvp", "writeup"],
            "agents": [
                {
                    "id": "paperforge-coordinator",
                    "title": "PaperForgeCoordinator",
                    "status": "runnable-bridge",
                },
                {
                    "id": "scientist-workflow-agent",
                    "title": "ScientistWorkflowAgent",
                    "status": "runnable-bridge",
                },
                {
                    "id": "mvp-workflow-agent",
                    "title": "MvpWorkflowAgent",
                    "status": "runnable-bridge",
                },
                {
                    "id": "writeup-agent",
                    "title": "WriteupAgent",
                    "status": "runnable-bridge",
                },
            ],
            "schemas": {
                "coordinator": self.input_schema(),
                "scientist": self.scientist.input_schema(),
                "mvp": self.mvp.input_schema(),
                "writeup": self.writeup.input_schema(),
            },
        }

    def route_frontend_action(
        self,
        action: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        request = payload or {}
        normalized_action = (action or "").strip().lower()
        if normalized_action == "status_snapshot":
            return {
                "action": normalized_action,
                "accepted": True,
                "payload": self.status_snapshot(),
                "message": "Coordinator status snapshot ready.",
            }
        if normalized_action == "run_mvp":
            return {
                "action": normalized_action,
                "accepted": True,
                "payload": self.mvp.run(**request),
                "message": "MVP workflow request routed.",
            }
        if normalized_action == "run_scientist":
            return {
                "action": normalized_action,
                "accepted": True,
                "payload": self.scientist.run(**request),
                "message": "Scientist workflow request routed.",
            }
        if normalized_action == "run_writeup":
            return {
                "action": normalized_action,
                "accepted": True,
                "payload": self.writeup.run(**request),
                "message": "Writeup workflow request routed.",
            }
        return {
            "action": normalized_action,
            "accepted": False,
            "payload": request,
            "message": "Unsupported frontend action.",
        }
