from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
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


_WORKSPACE_PATTERN = re.compile(r"\[done\] workspace=(.+)")


@dataclass
class MvpWorkflowConfig:
    phase: str = "bootstrap"
    experiment: str = "paper_writer"
    run_dir: str | None = None
    idea_name: str = "paper_writer_mvp_pipeline"
    title: str = "Iterative Academic Paper Writing with Upload Feedback"
    description: str = "Generate draft, ingest uploaded server outputs, and iteratively refine the paper."
    engine: str = "openalex"
    writeup_model: str = "gpt-5.4-xhigh"
    gateway_profile: str | None = None
    existing_draft: str | None = None
    enforce_disclosure: bool | None = None
    skip_chktex_fix: bool | None = None
    python_bin: str | None = None
    skip_writeup: bool = False
    skip_mvp_run: bool = False
    bootstrap_run_index: int = 1
    optimize_runs: int = 2
    refine_profile: str = "balanced"
    refresh_literature: bool = False
    literature_top_k: int = 5
    run_cloud_cycle: bool = False
    cloud_run_dir: str | None = None
    pipeline_root: str | None = None
    pipeline_config: str | None = None
    pipeline_run_name: str = "final_full"
    pipeline_mode: str = "auto"
    pipeline_hardware_profile: str | None = None
    pipeline_device: str | None = None
    remote_config: str | None = None
    cloud_skip_run: bool = False
    cloud_skip_sync: bool = False
    execute: bool = False
    rubric_profile: str = "default"
    evidence_files: list[str] = field(default_factory=list)


def _append_flag(command: List[str], flag: str, enabled: bool) -> None:
    if enabled:
        command.append(flag)


def _append_option(command: List[str], flag: str, value: Any) -> None:
    if value is not None and str(value).strip():
        command.extend([flag, str(value)])


def _infer_workspace(stdout: str, fallback: Path | None) -> Path | None:
    match = _WORKSPACE_PATTERN.search(stdout)
    if match:
        return Path(match.group(1).strip()).expanduser().resolve()
    return fallback


def _phase_artifact_paths(workspace: Path | None) -> List[Path | None]:
    if workspace is None:
        return []
    return [
        workspace,
        workspace / "workflow_idea.json",
        workspace / "workflow_state.json",
        workspace / "notes.txt",
        workspace / "paper_mvp_draft.pdf",
        workspace / "paper_with_feedback.pdf",
        workspace / "paper_after_optimize.pdf",
        workspace / "paper_refined.pdf",
        workspace / "artifacts" / "upload_manifest.json",
        workspace / "artifacts" / "bibliography" / "library.json",
    ]


class MvpWorkflowAgent:
    """CLI bridge for the existing staged MVP workflow."""

    schema_name = "mvp_workflow_agent"

    def input_schema(self) -> Dict[str, Any]:
        return load_schema(self.schema_name)

    def build_config(self, **kwargs: Any) -> MvpWorkflowConfig:
        return MvpWorkflowConfig(
            phase=str(kwargs.get("phase", "bootstrap")),
            experiment=str(kwargs.get("experiment", "paper_writer")),
            run_dir=kwargs.get("run_dir"),
            idea_name=str(kwargs.get("idea_name", "paper_writer_mvp_pipeline")),
            title=str(
                kwargs.get(
                    "title",
                    "Iterative Academic Paper Writing with Upload Feedback",
                )
            ),
            description=str(
                kwargs.get(
                    "description",
                    "Generate draft, ingest uploaded server outputs, and iteratively refine the paper.",
                )
            ),
            engine=str(kwargs.get("engine", "openalex")),
            writeup_model=str(kwargs.get("writeup_model", "gpt-5.4-xhigh")),
            gateway_profile=kwargs.get("gateway_profile"),
            existing_draft=kwargs.get("existing_draft"),
            enforce_disclosure=kwargs.get("enforce_disclosure"),
            skip_chktex_fix=kwargs.get("skip_chktex_fix"),
            python_bin=kwargs.get("python_bin"),
            skip_writeup=bool(kwargs.get("skip_writeup", False)),
            skip_mvp_run=bool(kwargs.get("skip_mvp_run", False)),
            bootstrap_run_index=int(kwargs.get("bootstrap_run_index", 1)),
            optimize_runs=int(kwargs.get("optimize_runs", 2)),
            refine_profile=str(kwargs.get("refine_profile", "balanced")),
            refresh_literature=bool(kwargs.get("refresh_literature", False)),
            literature_top_k=int(kwargs.get("literature_top_k", 5)),
            run_cloud_cycle=bool(kwargs.get("run_cloud_cycle", False)),
            cloud_run_dir=kwargs.get("cloud_run_dir"),
            pipeline_root=kwargs.get("pipeline_root"),
            pipeline_config=kwargs.get("pipeline_config"),
            pipeline_run_name=str(kwargs.get("pipeline_run_name", "final_full")),
            pipeline_mode=str(kwargs.get("pipeline_mode", "auto")),
            pipeline_hardware_profile=kwargs.get("pipeline_hardware_profile"),
            pipeline_device=kwargs.get("pipeline_device"),
            remote_config=kwargs.get("remote_config"),
            cloud_skip_run=bool(kwargs.get("cloud_skip_run", False)),
            cloud_skip_sync=bool(kwargs.get("cloud_skip_sync", False)),
            execute=bool(kwargs.get("execute", False)),
            rubric_profile=str(kwargs.get("rubric_profile", "default")),
            evidence_files=list(kwargs.get("evidence_files", []) or []),
        )

    def build_command(self, cfg: MvpWorkflowConfig) -> List[str]:
        command = default_python_command("launch_mvp_workflow.py")
        command.extend(["--phase", cfg.phase, "--experiment", cfg.experiment])
        _append_option(command, "--run-dir", cfg.run_dir)
        _append_option(command, "--idea-name", cfg.idea_name)
        _append_option(command, "--title", cfg.title)
        _append_option(command, "--description", cfg.description)
        _append_option(command, "--engine", cfg.engine)
        _append_option(command, "--writeup-model", cfg.writeup_model)
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
        _append_option(command, "--python-bin", cfg.python_bin)
        _append_option(command, "--bootstrap-run-index", cfg.bootstrap_run_index)
        _append_option(command, "--optimize-runs", cfg.optimize_runs)
        _append_option(command, "--refine-profile", cfg.refine_profile)
        _append_option(command, "--literature-top-k", cfg.literature_top_k)
        _append_option(command, "--rubric-profile", cfg.rubric_profile)
        for evidence_file in cfg.evidence_files:
            _append_option(command, "--evidence-file", evidence_file)
        _append_option(command, "--cloud-run-dir", cfg.cloud_run_dir)
        _append_option(command, "--pipeline-root", cfg.pipeline_root)
        _append_option(command, "--pipeline-config", cfg.pipeline_config)
        _append_option(command, "--pipeline-run-name", cfg.pipeline_run_name)
        _append_option(command, "--pipeline-mode", cfg.pipeline_mode)
        _append_option(command, "--pipeline-hardware-profile", cfg.pipeline_hardware_profile)
        _append_option(command, "--pipeline-device", cfg.pipeline_device)
        _append_option(command, "--remote-config", cfg.remote_config)
        _append_flag(command, "--skip-writeup", cfg.skip_writeup)
        _append_flag(command, "--skip-mvp-run", cfg.skip_mvp_run)
        _append_flag(command, "--refresh-literature", cfg.refresh_literature)
        _append_flag(command, "--run-cloud-cycle", cfg.run_cloud_cycle)
        _append_flag(command, "--cloud-skip-run", cfg.cloud_skip_run)
        _append_flag(command, "--cloud-skip-sync", cfg.cloud_skip_sync)
        return command

    def run(self, **kwargs: Any) -> Dict[str, Any]:
        cfg = self.build_config(**kwargs)
        schema = self.input_schema()
        trace: List[Dict[str, Any]] = []
        command = self.build_command(cfg)
        workspace = normalize_path(cfg.run_dir)

        append_trace(trace, "validate", "ok", f"validated phase `{cfg.phase}`")
        append_trace(trace, "prepare", "ok", "prepared launch_mvp_workflow.py invocation")

        result = AgentBridgeResult(
            agent="MvpWorkflowAgent",
            entrypoint="launch_mvp_workflow.py",
            status="planned",
            input_schema=schema,
            input=asdict(cfg),
            command=command,
            workspace=str(workspace) if workspace else None,
            trace=trace,
            artifacts=existing_artifacts(_phase_artifact_paths(workspace)),
        )

        if not cfg.execute:
            return result.to_dict()

        append_trace(trace, "execute", "running", "launching mvp workflow")
        completed = execute_command(command, env=env_with_optional_system_python())
        workspace = _infer_workspace(completed.stdout, workspace)
        result.status = "completed" if completed.returncode == 0 else "failed"
        result.workspace = str(workspace) if workspace else None
        result.returncode = completed.returncode
        result.stdout = completed.stdout
        result.stderr = completed.stderr
        result.artifacts = existing_artifacts(_phase_artifact_paths(workspace))
        append_trace(
            trace,
            "execute",
            "ok" if completed.returncode == 0 else "error",
            f"process exited with code {completed.returncode}",
        )
        return result.to_dict()
