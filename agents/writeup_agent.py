from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List

from .runtime import (
    AgentBridgeResult,
    append_trace,
    default_python_command,
    env_with_optional_system_python,
    execute_command,
    existing_artifacts,
    load_schema,
    normalize_path,
)
from skills.writeup_bridge import resolve_writeup_phase_skills


@dataclass
class WriteupAgentConfig:
    workflow_kind: str = "mvp"
    workspace: str | None = None
    folder: str | None = None
    writeup_model: str = "gpt-5.4-xhigh"
    engine: str = "openalex"
    refine_profile: str = "balanced"
    gateway_profile: str | None = None
    existing_draft: str | None = None
    enforce_disclosure: bool | None = None
    skip_chktex_fix: bool | None = None
    no_writing: bool = False
    execute: bool = False


def _append_option(command: List[str], flag: str, value: Any) -> None:
    if value is not None and str(value).strip():
        command.extend([flag, str(value)])


def _append_flag(command: List[str], flag: str, enabled: bool) -> None:
    if enabled:
        command.append(flag)


def _writeup_artifacts(workspace: Path | None) -> List[Path | None]:
    if workspace is None:
        return []
    idea_name = workspace.name
    return [
        workspace,
        workspace / "notes.txt",
        workspace / "writeup_checkpoint.json",
        workspace / "latex" / "template.tex",
        workspace / "latex" / "checkpoints",
        workspace / f"{idea_name}.pdf",
        workspace / "paper_refined.pdf",
        workspace / "artifacts" / "bibliography" / "library.json",
    ]


class WriteupAgent:
    """Minimal bridge over the existing writeup entrypoints."""

    schema_name = "writeup_agent"

    def input_schema(self) -> Dict[str, Any]:
        return load_schema(self.schema_name)

    def build_config(self, **kwargs: Any) -> WriteupAgentConfig:
        return WriteupAgentConfig(
            workflow_kind=str(kwargs.get("workflow_kind", "mvp")),
            workspace=kwargs.get("workspace"),
            folder=kwargs.get("folder"),
            writeup_model=str(kwargs.get("writeup_model", "gpt-5.4-xhigh")),
            engine=str(kwargs.get("engine", "openalex")),
            refine_profile=str(kwargs.get("refine_profile", "balanced")),
            gateway_profile=kwargs.get("gateway_profile"),
            existing_draft=kwargs.get("existing_draft"),
            enforce_disclosure=kwargs.get("enforce_disclosure"),
            skip_chktex_fix=kwargs.get("skip_chktex_fix"),
            no_writing=bool(kwargs.get("no_writing", False)),
            execute=bool(kwargs.get("execute", False)),
        )

    def build_command(self, cfg: WriteupAgentConfig) -> List[str]:
        if cfg.workflow_kind == "scientist":
            folder = cfg.folder or cfg.workspace
            command = default_python_command("engine/perform_writeup.py")
            _append_option(command, "--folder", folder)
            _append_option(command, "--model", cfg.writeup_model)
            _append_option(command, "--engine", cfg.engine)
            _append_option(command, "--gateway-profile", cfg.gateway_profile)
            _append_option(command, "--existing-draft", cfg.existing_draft)
            if cfg.enforce_disclosure is True:
                command.append("--enforce-disclosure")
            if cfg.enforce_disclosure is False:
                command.append("--no-enforce-disclosure")
            if cfg.skip_chktex_fix is True:
                command.append("--skip-chktex-fix")
            if cfg.skip_chktex_fix is False:
                command.append("--no-skip-chktex-fix")
            _append_flag(command, "--no-writing", cfg.no_writing)
            return command

        workspace = cfg.workspace or cfg.folder
        command = default_python_command("launch_mvp_workflow.py")
        command.extend(["--phase", "refine"])
        _append_option(command, "--run-dir", workspace)
        _append_option(command, "--writeup-model", cfg.writeup_model)
        _append_option(command, "--engine", cfg.engine)
        _append_option(command, "--refine-profile", cfg.refine_profile)
        _append_option(command, "--gateway-profile", cfg.gateway_profile)
        _append_option(command, "--existing-draft", cfg.existing_draft)
        if cfg.enforce_disclosure is True:
            command.append("--enforce-disclosure")
        if cfg.enforce_disclosure is False:
            command.append("--no-enforce-disclosure")
        if cfg.skip_chktex_fix is True:
            command.append("--skip-chktex-fix")
        if cfg.skip_chktex_fix is False:
            command.append("--no-skip-chktex-fix")
        return command

    def run(self, **kwargs: Any) -> Dict[str, Any]:
        cfg = self.build_config(**kwargs)
        schema = self.input_schema()
        trace: List[Dict[str, Any]] = []
        command = self.build_command(cfg)
        workspace = normalize_path(cfg.workspace or cfg.folder)

        append_trace(trace, "validate", "ok", f"validated writeup target `{cfg.workflow_kind}`")
        append_trace(trace, "prepare", "ok", "prepared writeup bridge invocation")
        append_trace(
            trace,
            "skills",
            "ok",
            "writeup phase skills: " + ", ".join(resolve_writeup_phase_skills().keys()),
        )

        result = AgentBridgeResult(
            agent="WriteupAgent",
            entrypoint=command[1],
            status="planned",
            input_schema=schema,
            input={
                **asdict(cfg),
                "phase_skill_map": resolve_writeup_phase_skills(),
            },
            command=command,
            workspace=str(workspace) if workspace else None,
            trace=trace,
            artifacts=existing_artifacts(_writeup_artifacts(workspace)),
        )

        if not cfg.execute:
            return result.to_dict()

        append_trace(trace, "execute", "running", "launching writeup entrypoint")
        completed = execute_command(command, env=env_with_optional_system_python())
        result.status = "completed" if completed.returncode == 0 else "failed"
        result.returncode = completed.returncode
        result.stdout = completed.stdout
        result.stderr = completed.stderr
        result.artifacts = existing_artifacts(_writeup_artifacts(workspace))
        append_trace(
            trace,
            "execute",
            "ok" if completed.returncode == 0 else "error",
            f"process exited with code {completed.returncode}",
        )
        return result.to_dict()
