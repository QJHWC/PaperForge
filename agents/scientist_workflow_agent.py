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
)


@dataclass
class ScientistWorkflowConfig:
    experiment: str = "paper_writer"
    model: str = "claude-sonnet-4-6"
    idea_model: str | None = None
    code_model: str | None = None
    writeup_model: str | None = None
    review_model: str = "gpt-4o-2024-05-13"
    writeup: str = "latex"
    skip_idea_generation: bool = False
    skip_novelty_check: bool = False
    parallel: int = 0
    improvement: bool = False
    gpus: str | None = None
    num_ideas: int = 1
    engine: str = "openalex"
    execute: bool = False


def _append_flag(command: List[str], flag: str, enabled: bool) -> None:
    if enabled:
        command.append(flag)


def _append_option(command: List[str], flag: str, value: Any) -> None:
    if value is not None and str(value).strip():
        command.extend([flag, str(value)])


class ScientistWorkflowAgent:
    """CLI bridge for the existing scientist workflow."""

    schema_name = "scientist_workflow_agent"

    def input_schema(self) -> Dict[str, Any]:
        return load_schema(self.schema_name)

    def build_config(self, **kwargs: Any) -> ScientistWorkflowConfig:
        return ScientistWorkflowConfig(
            experiment=str(kwargs.get("experiment", "paper_writer")),
            model=str(kwargs.get("model", "claude-sonnet-4-6")),
            idea_model=kwargs.get("idea_model"),
            code_model=kwargs.get("code_model"),
            writeup_model=kwargs.get("writeup_model"),
            review_model=str(kwargs.get("review_model", "gpt-4o-2024-05-13")),
            writeup=str(kwargs.get("writeup", "latex")),
            skip_idea_generation=bool(kwargs.get("skip_idea_generation", False)),
            skip_novelty_check=bool(kwargs.get("skip_novelty_check", False)),
            parallel=int(kwargs.get("parallel", 0)),
            improvement=bool(kwargs.get("improvement", False)),
            gpus=kwargs.get("gpus"),
            num_ideas=int(kwargs.get("num_ideas", 1)),
            engine=str(kwargs.get("engine", "openalex")),
            execute=bool(kwargs.get("execute", False)),
        )

    def build_command(self, cfg: ScientistWorkflowConfig) -> List[str]:
        command = default_python_command("launch_scientist.py")
        _append_option(command, "--experiment", cfg.experiment)
        _append_option(command, "--model", cfg.model)
        _append_option(command, "--idea-model", cfg.idea_model)
        _append_option(command, "--code-model", cfg.code_model)
        _append_option(command, "--writeup-model", cfg.writeup_model)
        _append_option(command, "--review-model", cfg.review_model)
        _append_option(command, "--writeup", cfg.writeup)
        _append_option(command, "--parallel", cfg.parallel)
        _append_option(command, "--gpus", cfg.gpus)
        _append_option(command, "--num-ideas", cfg.num_ideas)
        _append_option(command, "--engine", cfg.engine)
        _append_flag(command, "--skip-idea-generation", cfg.skip_idea_generation)
        _append_flag(command, "--skip-novelty-check", cfg.skip_novelty_check)
        _append_flag(command, "--improvement", cfg.improvement)
        return command

    def run(self, **kwargs: Any) -> Dict[str, Any]:
        cfg = self.build_config(**kwargs)
        schema = self.input_schema()
        trace: List[Dict[str, Any]] = []
        command = self.build_command(cfg)
        results_root = (Path("results") / cfg.experiment).resolve()

        append_trace(trace, "validate", "ok", f"validated experiment `{cfg.experiment}`")
        append_trace(trace, "prepare", "ok", "prepared launch_scientist.py invocation")

        result = AgentBridgeResult(
            agent="ScientistWorkflowAgent",
            entrypoint="launch_scientist.py",
            status="planned",
            input_schema=schema,
            input=asdict(cfg),
            command=command,
            workspace=str(results_root),
            trace=trace,
            artifacts=existing_artifacts([results_root, Path("templates") / cfg.experiment / "ideas.json"]),
        )

        if not cfg.execute:
            return result.to_dict()

        append_trace(trace, "execute", "running", "launching scientist workflow")
        completed = execute_command(command, env=env_with_optional_system_python())
        result.status = "completed" if completed.returncode == 0 else "failed"
        result.returncode = completed.returncode
        result.stdout = completed.stdout
        result.stderr = completed.stderr
        result.artifacts = existing_artifacts([results_root, results_root / "ideas.json"])
        append_trace(
            trace,
            "execute",
            "ok" if completed.returncode == 0 else "error",
            f"process exited with code {completed.returncode}",
        )
        return result.to_dict()
