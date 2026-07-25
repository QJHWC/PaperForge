from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from engine.research_partner.contracts import (
    RESEARCH_PARTNER_CONTRACT_VERSION,
    RESEARCH_PARTNER_PLANNED_OUTPUT_KEYS,
    research_partner_artifact_paths,
    research_partner_output_contracts,
    research_partner_planned_outputs,
)
from engine.research_partner.proposal_bundle import materialize_proposal_bundle

from .mvp_workflow_agent import MvpWorkflowAgent, MvpWorkflowConfig
from .runtime import append_trace, coerce_bool, existing_artifacts, load_schema, normalize_path
from .scientist_workflow_agent import ScientistWorkflowAgent
from .writeup_agent import WriteupAgent

_EMBEDDED_POSIX_PATH_PATTERN = re.compile(r'(^|[=\s\'"(\[{])(/[^\s\'"\\\)\]\}]+)')


@dataclass
class CoordinatorResult:
    mode: str
    payload: dict[str, Any] = field(default_factory=dict)


class PaperForgeCoordinator:
    """Top-level router for the bridged agent layer."""

    schema_name = "coordinator"
    research_partner_schema_name = "research_partner"
    research_partner_mode = "research_partner"
    research_partner_delegate_mode = "mvp"
    research_partner_forced_phase = "bootstrap"
    research_partner_planned_output_keys = list(RESEARCH_PARTNER_PLANNED_OUTPUT_KEYS)

    def __init__(self) -> None:
        self.scientist = ScientistWorkflowAgent()
        self.mvp = MvpWorkflowAgent()
        self.writeup = WriteupAgent()

    def input_schema(self) -> dict[str, Any]:
        return load_schema(self.schema_name)

    def research_partner_schema(self) -> dict[str, Any]:
        return load_schema(self.research_partner_schema_name)

    def _status_modes(self) -> list[str]:
        return ["scientist", "mvp", "writeup", self.research_partner_mode]

    def _status_schemas(self) -> dict[str, Any]:
        return {
            "coordinator": self.input_schema(),
            "scientist": self.scientist.input_schema(),
            "mvp": self.mvp.input_schema(),
            "writeup": self.writeup.input_schema(),
            self.research_partner_mode: self.research_partner_schema(),
        }

    def _research_partner_snapshot(self) -> dict[str, Any]:
        return {
            "contract_version": self._research_partner_contract_version(),
            "output_contracts": self._research_partner_planned_output_contracts(),
            "mode_behavior": self._research_partner_mode_behavior(),
            "planned_workflow": self._research_partner_planned_workflow(),
        }

    def _research_partner_request(self, **kwargs: Any) -> dict[str, Any]:
        cfg = MvpWorkflowConfig(
            phase=self.research_partner_forced_phase,
            experiment=str(kwargs.get("experiment", "paper_writer")),
            run_dir=kwargs.get("run_dir"),
            idea_name=str(kwargs.get("idea_name", "paper_writer_research_partner")),
            title=str(kwargs.get("title", "Evidence-first Research Partner Session")),
            description=str(
                kwargs.get(
                    "description",
                    "Bootstrap an evidence-first workspace for search, idea generation, and critique.",
                )
            ),
            engine=str(kwargs.get("engine", "openalex")),
            refresh_literature=True,
            literature_top_k=int(kwargs.get("literature_top_k", 5)),
            rubric_profile=str(kwargs.get("rubric_profile", "default")),
            evidence_files=list(kwargs.get("evidence_files", []) or []),
            skip_mvp_run=True,
            skip_writeup=True,
            execute=coerce_bool(kwargs.get("execute", False)),
        )
        return asdict(cfg)

    def _research_partner_public_input(self, request: dict[str, Any], workspace: Path | None) -> dict[str, Any]:
        evidence_files = [PurePosixPath(str(item)).name or "[redacted]" for item in request.get("evidence_files", [])]
        run_dir = request["run_dir"]
        if run_dir and Path(str(run_dir)).expanduser().is_absolute():
            run_dir = self._sanitize_workspace_reference(workspace)
        return {
            "experiment": request["experiment"],
            "run_dir": run_dir,
            "engine": request["engine"],
            "idea_name": request["idea_name"],
            "title": request["title"],
            "description": request["description"],
            "literature_top_k": request["literature_top_k"],
            "rubric_profile": request["rubric_profile"],
            "evidence_files": evidence_files,
            "execute": request["execute"],
        }

    def _research_partner_mode_behavior(self) -> dict[str, Any]:
        return {
            "delegates_to": self.research_partner_delegate_mode,
            "forced_phase": self.research_partner_forced_phase,
            "artifact_mode": "planned_only",
            "planned_output_keys": list(self.research_partner_planned_output_keys),
        }

    def _research_partner_planned_workflow(self) -> list[dict[str, Any]]:
        return [
            {
                "id": "search",
                "title": "Literature search bootstrap",
                "status": "planned",
                "planned_outputs": [],
            },
            {
                "id": "idea",
                "title": "Idea framing",
                "status": "planned",
                "planned_outputs": ["idea_brief", "claim_graph", "experiment_blueprint"],
            },
            {
                "id": "critique",
                "title": "Critique drafting",
                "status": "planned",
                "planned_outputs": [
                    "critique_report",
                    "proposal_brief",
                    "expected_figures",
                    "rubric_scorecard",
                    "evidence_index",
                    "manifest",
                ],
            },
        ]

    def _research_partner_planned_output_contracts(self) -> dict[str, Any]:
        return research_partner_output_contracts()

    def _research_partner_contract_version(self) -> str:
        return RESEARCH_PARTNER_CONTRACT_VERSION

    def _research_partner_planned_outputs(
        self,
        workspace: Path | None,
        *,
        materialized: bool = False,
    ) -> dict[str, dict[str, Any]]:
        return research_partner_planned_outputs(workspace, materialized=materialized)

    def _sanitize_artifacts(self, workspace: Path | None, artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        sanitized: list[dict[str, Any]] = []
        for artifact in artifacts:
            record = dict(artifact)
            path_value = str(record.get("path") or "").strip()
            if path_value:
                record["path"] = self._sanitize_workspace_path(workspace, path_value)
            sanitized.append(record)
        return sanitized

    def _sanitize_workspace_path(self, workspace: Path | None, path_value: str | Path) -> str:
        candidate = Path(path_value).expanduser()
        raw_value = str(path_value).strip()
        resolved = (
            candidate.resolve()
            if candidate.is_absolute() or raw_value.startswith("/")
            else candidate
        )
        if workspace is not None:
            try:
                return resolved.relative_to(workspace).as_posix()
            except ValueError:
                pass
        name = resolved.name.strip()
        return name or "[redacted]"

    def _sanitize_workspace_reference(self, workspace: Path | None) -> str | None:
        if workspace is None:
            return None
        return "."

    def _sanitize_error_detail(self, detail: str) -> str:
        sanitized = str(detail or "")
        while True:
            updated = _EMBEDDED_POSIX_PATH_PATTERN.sub(
                lambda match: f"{match.group(1)}{self._sanitize_workspace_path(None, match.group(2))}",
                sanitized,
            )
            if updated == sanitized:
                return updated
            sanitized = updated

    def _sanitize_output_text(self, workspace: Path | None, text: str) -> str:
        sanitized = str(text or "")
        while True:
            updated = _EMBEDDED_POSIX_PATH_PATTERN.sub(
                lambda match: f"{match.group(1)}{self._sanitize_workspace_path(workspace, match.group(2))}",
                sanitized,
            )
            if updated == sanitized:
                return updated
            sanitized = updated

    def _materialization_error_detail(self, exc: Exception) -> str:
        detail = self._sanitize_error_detail(str(exc).splitlines()[0]) if str(exc).strip() else ""
        if detail and detail != exc.__class__.__name__:
            return f"research_partner materialization failed: {exc.__class__.__name__}: {detail}"
        return f"research_partner materialization failed: {exc.__class__.__name__}"

    def _run_research_partner(self, **kwargs: Any) -> dict[str, Any]:
        request = self._research_partner_request(**kwargs)
        payload = self.mvp.run(**request)
        workspace = normalize_path(payload.get("workspace"))
        materialized = False

        if request["execute"] and payload.get("status") == "completed" and workspace is not None:
            try:
                materialize_proposal_bundle(
                    workspace=workspace,
                    title=request["title"],
                    description=request["description"],
                    rubric_profile=request["rubric_profile"],
                    evidence_files=list(request.get("evidence_files", [])),
                )
            except ValueError as exc:
                payload["status"] = "failed"
                append_trace(
                    payload.setdefault("trace", []),
                    "proposal_materialize",
                    "error",
                    self._sanitize_error_detail(str(exc)),
                )
            except Exception as exc:
                payload["status"] = "failed"
                append_trace(
                    payload.setdefault("trace", []),
                    "proposal_materialize",
                    "error",
                    self._materialization_error_detail(exc),
                )
            else:
                append_trace(
                    payload.setdefault("trace", []),
                    "proposal_materialize",
                    "ok",
                    "materialized research partner proposal bundle",
                )
                artifacts = list(payload.get("artifacts", []))
                artifacts.extend(existing_artifacts(research_partner_artifact_paths(workspace)))
                deduped_artifacts: list[dict[str, Any]] = []
                seen_paths: set[str] = set()
                for artifact in artifacts:
                    path = str(artifact.get("path") or "")
                    if path and path not in seen_paths:
                        seen_paths.add(path)
                        deduped_artifacts.append(artifact)
                payload["artifacts"] = deduped_artifacts
                materialized = True

        snapshot = self._research_partner_snapshot()
        sanitized_workspace = self._sanitize_workspace_reference(workspace)
        payload["workspace"] = sanitized_workspace
        payload["stdout"] = self._sanitize_output_text(workspace, str(payload.get("stdout", "")))
        payload["stderr"] = self._sanitize_output_text(workspace, str(payload.get("stderr", "")))
        payload["artifacts"] = self._sanitize_artifacts(workspace, list(payload.get("artifacts", [])))
        return {
            **payload,
            "input_schema": self.research_partner_schema(),
            "input": self._research_partner_public_input(request, workspace),
            "contract_version": snapshot["contract_version"],
            "mode_behavior": deepcopy(snapshot["mode_behavior"]),
            "planned_workflow": deepcopy(snapshot["planned_workflow"]),
            "planned_output_contracts": deepcopy(snapshot["output_contracts"]),
            "planned_outputs": self._research_partner_planned_outputs(workspace, materialized=materialized),
        }

    def run(self, mode: str, **kwargs: Any) -> CoordinatorResult:
        normalized_mode = (mode or "").strip().lower()
        if normalized_mode == "scientist":
            return CoordinatorResult(mode="scientist", payload=self.scientist.run(**kwargs))
        if normalized_mode == "mvp":
            return CoordinatorResult(mode="mvp", payload=self.mvp.run(**kwargs))
        if normalized_mode == "writeup":
            return CoordinatorResult(mode="writeup", payload=self.writeup.run(**kwargs))
        if normalized_mode == self.research_partner_mode:
            return CoordinatorResult(mode=self.research_partner_mode, payload=self._run_research_partner(**kwargs))
        raise ValueError(f"Unsupported mode: {mode}")

    def status_snapshot(self) -> dict[str, Any]:
        snapshot = self._research_partner_snapshot()
        return {
            "modes": self._status_modes(),
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
            "schemas": self._status_schemas(),
            "contract_versions": {
                self.research_partner_mode: snapshot["contract_version"],
            },
            "output_contracts": {
                self.research_partner_mode: deepcopy(snapshot["output_contracts"]),
            },
            "mode_behaviors": {
                self.research_partner_mode: deepcopy(snapshot["mode_behavior"]),
            },
            "planned_workflows": {
                self.research_partner_mode: deepcopy(snapshot["planned_workflow"]),
            },
        }

    def route_frontend_action(
        self,
        action: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
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
        if normalized_action == "run_research_partner":
            return {
                "action": normalized_action,
                "accepted": True,
                "payload": self._run_research_partner(**request),
                "message": "Research partner workflow request routed.",
            }
        return {
            "action": normalized_action,
            "accepted": False,
            "payload": request,
            "message": "Unsupported frontend action.",
        }
