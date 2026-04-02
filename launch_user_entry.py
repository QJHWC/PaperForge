#!/usr/bin/env python3
from __future__ import annotations

import argparse
from contextlib import contextmanager
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterable, Iterator, Optional

from engine.research_partner.proposal_bundle import materialize_proposal_bundle


ROOT = Path(__file__).resolve().parent
_WORKSPACE_DONE_PATTERN = re.compile(r"\[done\]\s+workspace=(.+)")
_EMBEDDED_POSIX_PATH_PATTERN = re.compile(r'(^|[=\s\'"(\[{])(/[^\s\'"\)\]\}]+)')
_MATERIALIZATION_ENV_KEYS = {
    "PAPERFORGE_CLAUDE_OPENAI_COMPAT",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_WRITEUP_API_KEY",
    "OPENAI_WRITEUP_BASE_URL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
}


def _sanitize_cli_text(text: str) -> str:
    sanitized = text.replace(str(ROOT), ".")
    sanitized = sanitized.replace(str(ROOT.resolve()), ".")
    return sanitized


def _sanitize_path_value(value: str) -> str:
    cleaned = _sanitize_cli_text(str(value).strip())
    if not cleaned:
        return cleaned
    if cleaned.startswith("~/"):
        cleaned = str(Path(cleaned).expanduser())
    if cleaned.startswith("/"):
        name = Path(cleaned).name.strip()
        return name or "[redacted]"
    return cleaned


def _sanitize_embedded_paths(text: str) -> str:
    sanitized = text
    while True:
        updated = _EMBEDDED_POSIX_PATH_PATTERN.sub(
            lambda match: f"{match.group(1)}{_sanitize_path_value(match.group(2))}",
            sanitized,
        )
        if updated == sanitized:
            return updated
        sanitized = updated


def _sanitize_cli_value(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        rendered = ", ".join(_sanitize_cli_value(item) for item in value)
        return f"[{rendered}]"
    return _sanitize_embedded_paths(str(value))


def _materialization_env(env: Dict[str, str]) -> Dict[str, str]:
    return {key: env_value for key, env_value in env.items() if key in _MATERIALIZATION_ENV_KEYS}


@contextmanager
def _temporary_env(overrides: Dict[str, str]) -> Iterator[None]:
    previous = {key: os.environ.get(key) for key in overrides}
    try:
        for key, value in overrides.items():
            os.environ[key] = value
        yield
    finally:
        for key, previous_value in previous.items():
            if previous_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous_value


def _in_virtualenv() -> bool:
    return bool(getattr(sys, "base_prefix", sys.prefix) != sys.prefix)


def _require_virtualenv(script_name: str) -> None:
    allow_system = os.getenv("PAPERFORGE_ALLOW_SYSTEM_PYTHON", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if allow_system or _in_virtualenv():
        return

    raise SystemExit(
        "环境错误: 检测到正在使用系统 Python 运行 PaperForge。\n"
        f"当前解释器: {sys.executable}\n"
        "请先激活虚拟环境后再运行，例如:\n"
        f"  source .venv311/bin/activate && python {script_name} --help\n"
        "如需强制跳过校验，可设置 PAPERFORGE_ALLOW_SYSTEM_PYTHON=1。"
    )


def _normalize_base_url(url: str) -> str:
    cleaned = url.strip().rstrip("/")
    if cleaned.endswith("/v1"):
        return cleaned
    return f"{cleaned}/v1"


def _clean_optional(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _resolve_config_value(
    cli_value: Optional[str],
    env_names: Iterable[str],
    *,
    normalizer: Optional[Callable[[str], str]] = None,
) -> Dict[str, Optional[str]]:
    value = _clean_optional(cli_value)
    source = "cli"

    if value is None:
        source = "default"
        for env_name in env_names:
            env_value = _clean_optional(os.getenv(env_name))
            if env_value is not None:
                value = env_value
                source = f"env:{env_name}"
                break

    if value is not None and normalizer is not None:
        value = normalizer(value)

    return {"value": value, "source": source}


def _collect_effective_config(args: argparse.Namespace) -> Dict[str, Dict[str, Optional[str]]]:
    return {
        "openai_api_key": _resolve_config_value(args.openai_api_key, ("OPENAI_API_KEY",)),
        "openai_base_url": _resolve_config_value(
            args.openai_base_url,
            ("OPENAI_BASE_URL", "OPENAI_API_BASE"),
            normalizer=_normalize_base_url,
        ),
        "openai_writeup_api_key": _resolve_config_value(
            args.openai_writeup_api_key,
            ("OPENAI_WRITEUP_API_KEY",),
        ),
        "openai_writeup_base_url": _resolve_config_value(
            args.openai_writeup_base_url,
            ("OPENAI_WRITEUP_BASE_URL",),
            normalizer=_normalize_base_url,
        ),
        "anthropic_api_key": _resolve_config_value(args.anthropic_api_key, ("ANTHROPIC_API_KEY",)),
        "anthropic_auth_token": _resolve_config_value(None, ("ANTHROPIC_AUTH_TOKEN",)),
        "anthropic_base_url": _resolve_config_value(args.anthropic_base_url, ("ANTHROPIC_BASE_URL",)),
        "writeup_cite_rounds": _resolve_config_value(None, ("WRITEUP_CITE_ROUNDS",)),
        "writeup_latex_fix_rounds": _resolve_config_value(None, ("WRITEUP_LATEX_FIX_ROUNDS",)),
        "writeup_second_refinement": _resolve_config_value(None, ("WRITEUP_SECOND_REFINEMENT",)),
    }


def _is_claude_model_name(model: Optional[str]) -> bool:
    return bool(model and "claude" in model.lower())


def _uses_claude_models(args: argparse.Namespace) -> bool:
    if getattr(args, "entry", None) == "research_partner":
        return True
    model_candidates = [
        getattr(args, "model", None),
        getattr(args, "idea_model", None),
        getattr(args, "code_model", None),
        getattr(args, "writeup_model", None),
        getattr(args, "review_model", None),
    ]
    return any(_is_claude_model_name(model) for model in model_candidates)


def _validate_effective_config(args: argparse.Namespace, cfg: Dict[str, Dict[str, Optional[str]]]) -> None:
    if not _uses_claude_models(args):
        return

    if args.claude_protocol == "openai":
        if not (cfg["openai_api_key"]["value"] or cfg["openai_writeup_api_key"]["value"]):
            raise SystemExit(
                "配置错误: --claude-protocol openai 需要 OpenAI key。"
                "请通过 --openai-api-key / --openai-writeup-api-key 或 "
                "OPENAI_API_KEY / OPENAI_WRITEUP_API_KEY 提供。"
            )
        return

    if not (cfg["anthropic_api_key"]["value"] or cfg["anthropic_auth_token"]["value"]):
        raise SystemExit(
            "配置错误: --claude-protocol anthropic 需要 Anthropic 凭据。"
            "请通过 --anthropic-api-key 或 ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN 提供。"
        )


def _apply_api_env(args: argparse.Namespace, cfg: Dict[str, Dict[str, Optional[str]]]) -> dict:
    env = os.environ.copy()

    # Route Claude models via native Anthropic API or OpenAI-compatible API.
    env["PAPERFORGE_CLAUDE_OPENAI_COMPAT"] = "1" if args.claude_protocol == "openai" else "0"

    if cfg["openai_api_key"]["value"]:
        env["OPENAI_API_KEY"] = str(cfg["openai_api_key"]["value"])
    if cfg["openai_base_url"]["value"]:
        env["OPENAI_BASE_URL"] = str(cfg["openai_base_url"]["value"])
    if cfg["openai_writeup_api_key"]["value"]:
        env["OPENAI_WRITEUP_API_KEY"] = str(cfg["openai_writeup_api_key"]["value"])
    if cfg["openai_writeup_base_url"]["value"]:
        env["OPENAI_WRITEUP_BASE_URL"] = str(cfg["openai_writeup_base_url"]["value"])
    if cfg["anthropic_api_key"]["value"]:
        env["ANTHROPIC_API_KEY"] = str(cfg["anthropic_api_key"]["value"])
    if cfg["anthropic_base_url"]["value"]:
        env["ANTHROPIC_BASE_URL"] = str(cfg["anthropic_base_url"]["value"])
    if getattr(args, "gateway_profile", None):
        env["PAPERFORGE_GATEWAY_PROFILE"] = str(args.gateway_profile)

    return env


def _mask_secret(value: Optional[str]) -> str:
    if not value:
        return "(unset)"
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}...{value[-4:]}"


def _display_value(value: Optional[str], *, empty_fallback: str = "(default)") -> str:
    if value is None:
        return empty_fallback
    return str(value)


def _print_effective_configuration(
    args: argparse.Namespace,
    cfg: Dict[str, Dict[str, Optional[str]]],
    cmd: list[str],
    passthrough: list[str],
) -> None:
    print("[entry]", args.entry)
    print("[claude_protocol]", args.claude_protocol)
    print("[command]", " ".join(_sanitize_cli_value(part) for part in cmd))
    if passthrough:
        print("[passthrough]", " ".join(_sanitize_cli_value(part) for part in passthrough))

    print("[config] precedence: CLI > ENV > default")
    for arg_name in sorted(vars(args)):
        if arg_name in {
            "openai_api_key",
            "openai_writeup_api_key",
            "anthropic_api_key",
        }:
            continue
        print(f"[config][arg] {arg_name}={_sanitize_cli_value(getattr(args, arg_name))}")

    print(
        "[config][api] OPENAI_API_KEY=",
        _mask_secret(cfg["openai_api_key"]["value"]),
        f"({cfg['openai_api_key']['source']})",
        sep="",
    )
    print(
        "[config][api] OPENAI_BASE_URL=",
        _display_value(cfg["openai_base_url"]["value"], empty_fallback="(unset)"),
        f"({cfg['openai_base_url']['source']})",
        sep="",
    )
    print(
        "[config][api] OPENAI_WRITEUP_API_KEY=",
        _mask_secret(cfg["openai_writeup_api_key"]["value"]),
        f"({cfg['openai_writeup_api_key']['source']})",
        sep="",
    )
    print(
        "[config][api] OPENAI_WRITEUP_BASE_URL=",
        _display_value(cfg["openai_writeup_base_url"]["value"], empty_fallback="(unset)"),
        f"({cfg['openai_writeup_base_url']['source']})",
        sep="",
    )
    print(
        "[config][api] ANTHROPIC_API_KEY=",
        _mask_secret(cfg["anthropic_api_key"]["value"]),
        f"({cfg['anthropic_api_key']['source']})",
        sep="",
    )
    print(
        "[config][api] ANTHROPIC_AUTH_TOKEN=",
        _mask_secret(cfg["anthropic_auth_token"]["value"]),
        f"({cfg['anthropic_auth_token']['source']})",
        sep="",
    )
    print(
        "[config][api] ANTHROPIC_BASE_URL=",
        _display_value(cfg["anthropic_base_url"]["value"], empty_fallback="(unset)"),
        f"({cfg['anthropic_base_url']['source']})",
        sep="",
    )
    print(
        "[config][writeup] WRITEUP_CITE_ROUNDS=",
        _display_value(cfg["writeup_cite_rounds"]["value"], empty_fallback="(downstream default)"),
        f"({cfg['writeup_cite_rounds']['source']})",
        sep="",
    )
    print(
        "[config][writeup] WRITEUP_LATEX_FIX_ROUNDS=",
        _display_value(cfg["writeup_latex_fix_rounds"]["value"], empty_fallback="(downstream default)"),
        f"({cfg['writeup_latex_fix_rounds']['source']})",
        sep="",
    )
    print(
        "[config][writeup] WRITEUP_SECOND_REFINEMENT=",
        _display_value(cfg["writeup_second_refinement"]["value"], empty_fallback="(downstream default)"),
        f"({cfg['writeup_second_refinement']['source']})",
        sep="",
    )


def _append_flag(cmd: list[str], flag: str, enabled: bool) -> None:
    if enabled:
        cmd.append(flag)


def _append_opt(cmd: list[str], flag: str, value) -> None:
    if value is not None:
        cmd.extend([flag, str(value)])


def _build_scientist_cmd(args: argparse.Namespace, passthrough: list[str]) -> list[str]:
    cmd = [sys.executable, str(ROOT / "launch_scientist.py")]
    _append_opt(cmd, "--experiment", args.experiment)
    _append_opt(cmd, "--num-ideas", args.num_ideas)
    _append_opt(cmd, "--engine", args.engine)
    _append_opt(cmd, "--parallel", args.parallel)
    _append_opt(cmd, "--gpus", args.gpus)
    _append_opt(cmd, "--writeup", args.writeup)

    _append_opt(cmd, "--model", args.model)
    _append_opt(cmd, "--idea-model", args.idea_model)
    _append_opt(cmd, "--code-model", args.code_model)
    _append_opt(cmd, "--writeup-model", args.writeup_model)
    _append_opt(cmd, "--review-model", args.review_model)
    _append_opt(cmd, "--gateway-profile", getattr(args, "gateway_profile", None))

    _append_flag(cmd, "--skip-idea-generation", args.skip_idea_generation)
    _append_flag(cmd, "--skip-novelty-check", args.skip_novelty_check)
    _append_flag(cmd, "--improvement", args.improvement)

    cmd.extend(passthrough)
    return cmd


def _build_mvp_cmd(args: argparse.Namespace, passthrough: list[str]) -> list[str]:
    cmd = [sys.executable, str(ROOT / "launch_mvp_workflow.py")]
    _append_opt(cmd, "--phase", args.phase)
    _append_opt(cmd, "--experiment", args.experiment)
    _append_opt(cmd, "--run-dir", args.run_dir)
    _append_opt(cmd, "--engine", args.engine)
    _append_opt(cmd, "--writeup-model", args.writeup_model)
    _append_opt(cmd, "--gateway-profile", getattr(args, "gateway_profile", None))
    _append_opt(cmd, "--existing-draft", getattr(args, "existing_draft", None))
    _append_opt(cmd, "--optimize-runs", args.optimize_runs)
    _append_opt(cmd, "--refine-profile", args.refine_profile)
    _append_opt(cmd, "--idea-name", args.idea_name)
    _append_opt(cmd, "--title", args.title)
    _append_opt(cmd, "--description", args.description)
    if getattr(args, "enforce_disclosure", None) is True:
        cmd.append("--enforce-disclosure")
    if getattr(args, "enforce_disclosure", None) is False:
        cmd.append("--no-enforce-disclosure")
    if getattr(args, "skip_chktex_fix", None) is True:
        cmd.append("--skip-chktex-fix")
    if getattr(args, "skip_chktex_fix", None) is False:
        cmd.append("--no-skip-chktex-fix")

    _append_flag(cmd, "--skip-writeup", args.skip_writeup)
    _append_flag(cmd, "--skip-mvp-run", args.skip_mvp_run)
    _append_flag(cmd, "--refresh-literature", args.refresh_literature)
    _append_flag(cmd, "--run-cloud-cycle", args.run_cloud_cycle)
    _append_flag(cmd, "--cloud-skip-run", args.cloud_skip_run)
    _append_flag(cmd, "--cloud-skip-sync", args.cloud_skip_sync)

    _append_opt(cmd, "--cloud-run-dir", args.cloud_run_dir)
    _append_opt(cmd, "--pipeline-root", args.pipeline_root)
    _append_opt(cmd, "--pipeline-config", args.pipeline_config)
    _append_opt(cmd, "--pipeline-run-name", args.pipeline_run_name)
    _append_opt(cmd, "--pipeline-mode", args.pipeline_mode)
    _append_opt(cmd, "--pipeline-hardware-profile", args.pipeline_hardware_profile)
    _append_opt(cmd, "--pipeline-device", args.pipeline_device)

    cmd.extend(passthrough)
    return cmd


def _build_writeup_cmd(args: argparse.Namespace, passthrough: list[str]) -> list[str]:
    if passthrough:
        extras = " ".join(passthrough)
        raise SystemExit(f"writeup 不支持额外参数: {extras}")

    if args.workflow_kind == "scientist":
        cmd = [sys.executable, str(ROOT / "engine" / "perform_writeup.py")]
        _append_opt(cmd, "--folder", args.folder or args.workspace)
        _append_opt(cmd, "--model", args.writeup_model)
        _append_opt(cmd, "--engine", args.engine)
        if args.no_writing:
            cmd.append("--no-writing")
        return cmd

    cmd = [sys.executable, str(ROOT / "launch_mvp_workflow.py")]
    cmd.extend(["--phase", "refine"])
    _append_opt(cmd, "--run-dir", args.workspace or args.folder)
    _append_opt(cmd, "--writeup-model", args.writeup_model)
    _append_opt(cmd, "--engine", args.engine)
    _append_opt(cmd, "--refine-profile", args.refine_profile)
    _append_opt(cmd, "--gateway-profile", getattr(args, "gateway_profile", None))
    _append_opt(cmd, "--existing-draft", getattr(args, "existing_draft", None))
    if getattr(args, "enforce_disclosure", None) is True:
        cmd.append("--enforce-disclosure")
    if getattr(args, "enforce_disclosure", None) is False:
        cmd.append("--no-enforce-disclosure")
    if getattr(args, "skip_chktex_fix", None) is True:
        cmd.append("--skip-chktex-fix")
    if getattr(args, "skip_chktex_fix", None) is False:
        cmd.append("--no-skip-chktex-fix")
    return cmd


def _build_research_cmd(args: argparse.Namespace, passthrough: list[str]) -> list[str]:
    if passthrough:
        extras = " ".join(passthrough)
        raise SystemExit(f"research_partner 不支持额外参数: {extras}")
    cmd = [sys.executable, str(ROOT / "launch_mvp_workflow.py")]
    _append_opt(cmd, "--phase", "bootstrap")
    _append_opt(cmd, "--experiment", args.experiment)
    _append_opt(cmd, "--run-dir", args.run_dir)
    _append_opt(cmd, "--engine", args.engine)
    _append_opt(cmd, "--idea-name", args.idea_name)
    _append_opt(cmd, "--title", args.title)
    _append_opt(cmd, "--description", args.description)
    _append_opt(cmd, "--literature-top-k", args.literature_top_k)
    _append_opt(cmd, "--rubric-profile", args.rubric_profile)
    for evidence_file in getattr(args, "evidence_file", []) or []:
        _append_opt(cmd, "--evidence-file", evidence_file)
    _append_flag(cmd, "--skip-mvp-run", True)
    _append_flag(cmd, "--skip-writeup", True)
    _append_flag(cmd, "--refresh-literature", True)
    return cmd


def _materialization_error_message(exc: Exception) -> str:
    detail = _sanitize_cli_value(str(exc).splitlines()[0]) if str(exc).strip() else ""
    if detail and detail != exc.__class__.__name__:
        return f"research_partner materialization failed: {exc.__class__.__name__}: {detail}"
    return f"research_partner materialization failed: {exc.__class__.__name__}"


def _maybe_materialize_research_partner(
    *,
    entry: str,
    args: argparse.Namespace,
    completed,
    materialize_env: Optional[Dict[str, str]] = None,
) -> None:
    if entry != "research_partner" or getattr(completed, "returncode", 1) != 0:
        return
    explicit_run_dir = _clean_optional(getattr(args, "run_dir", None))
    if explicit_run_dir is not None:
        workspace = (ROOT / explicit_run_dir).expanduser().resolve()
    else:
        stdout = str(getattr(completed, "stdout", "") or "")
        match = _WORKSPACE_DONE_PATTERN.search(stdout)
        if not match:
            return
        workspace_value = match.group(1).strip().replace("\\r", "").replace("\\n", "")
        workspace = Path(workspace_value).expanduser().resolve()
    try:
        with _temporary_env(materialize_env or {}):
            materialize_proposal_bundle(
                workspace=workspace,
                title=args.title,
                description=args.description,
                rubric_profile=args.rubric_profile,
                evidence_files=list(getattr(args, "evidence_file", []) or []),
            )
    except ValueError as exc:
        raise SystemExit(_sanitize_cli_value(str(exc))) from exc
    except Exception as exc:
        raise SystemExit(_materialization_error_message(exc)) from exc


def _add_common_api_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--claude-protocol",
        choices=["anthropic", "openai"],
        default="anthropic",
        help="Claude 模型走原生 Anthropic 协议，或走 OpenAI 兼容协议。",
    )
    parser.add_argument("--openai-api-key", default=None)
    parser.add_argument("--openai-base-url", default=None)
    parser.add_argument("--openai-writeup-api-key", default=None)
    parser.add_argument("--openai-writeup-base-url", default=None)
    parser.add_argument("--anthropic-api-key", default=None)
    parser.add_argument("--anthropic-base-url", default=None)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印将执行的命令与协议路由，不真正执行。",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="PaperForge 用户专用入口：统一配置 API 协议与分阶段模型后启动主流程。"
    )
    subparsers = parser.add_subparsers(dest="entry", required=True)

    scientist = subparsers.add_parser(
        "scientist",
        help="全自动主线：idea -> experiment -> writeup -> review",
    )
    _add_common_api_args(scientist)
    scientist.add_argument("--experiment", default="paper_writer")
    scientist.add_argument("--num-ideas", type=int, default=1)
    scientist.add_argument("--engine", choices=["semanticscholar", "openalex"], default="openalex")
    scientist.add_argument("--parallel", type=int, default=0)
    scientist.add_argument("--gpus", default=None)
    scientist.add_argument("--writeup", choices=["latex"], default="latex")
    scientist.add_argument("--model", default="gpt-5.4-xhigh")
    scientist.add_argument("--idea-model", default=None)
    scientist.add_argument("--code-model", default=None)
    scientist.add_argument("--writeup-model", default=None)
    scientist.add_argument("--review-model", default="gpt-5.4-xhigh")
    scientist.add_argument("--gateway-profile", choices=["safe", "full"], default=None)
    scientist.add_argument("--skip-idea-generation", action="store_true")
    scientist.add_argument("--skip-novelty-check", action="store_true")
    scientist.add_argument("--improvement", action="store_true")

    mvp = subparsers.add_parser(
        "mvp",
        help="分阶段主线：bootstrap/feedback/optimize/refine/cloud/all",
    )
    _add_common_api_args(mvp)
    mvp.add_argument("--phase", choices=["bootstrap", "feedback", "optimize", "refine", "cloud", "all"], default="bootstrap")
    mvp.add_argument("--experiment", default="paper_writer")
    mvp.add_argument("--run-dir", default=None)
    mvp.add_argument("--engine", choices=["semanticscholar", "openalex"], default="openalex")
    mvp.add_argument("--writeup-model", default=None)
    mvp.add_argument("--gateway-profile", choices=["safe", "full"], default=None)
    mvp.add_argument("--existing-draft", default=None)
    mvp.add_argument("--optimize-runs", type=int, default=2)
    mvp.add_argument("--refine-profile", choices=["fast", "balanced", "deep"], default="balanced")
    mvp.add_argument("--idea-name", default="paper_writer_user_entry")
    mvp.add_argument("--title", default="Iterative Academic Paper Writing with User API Entry")
    mvp.add_argument(
        "--description",
        default="User entry with selectable API protocols and stage models.",
    )
    mvp.add_argument("--skip-writeup", action="store_true")
    mvp.add_argument("--skip-mvp-run", action="store_true")
    mvp.add_argument("--enforce-disclosure", dest="enforce_disclosure", action="store_true")
    mvp.add_argument("--no-enforce-disclosure", dest="enforce_disclosure", action="store_false")
    mvp.add_argument("--skip-chktex-fix", dest="skip_chktex_fix", action="store_true")
    mvp.add_argument("--no-skip-chktex-fix", dest="skip_chktex_fix", action="store_false")
    mvp.set_defaults(enforce_disclosure=None, skip_chktex_fix=None)
    mvp.add_argument("--refresh-literature", action="store_true")
    mvp.add_argument("--run-cloud-cycle", action="store_true")
    mvp.add_argument("--cloud-skip-run", action="store_true")
    mvp.add_argument("--cloud-skip-sync", action="store_true")
    mvp.add_argument("--cloud-run-dir", default=None)
    mvp.add_argument("--pipeline-root", default=None)
    mvp.add_argument("--pipeline-config", default=None)
    mvp.add_argument("--pipeline-run-name", default=None)
    mvp.add_argument("--pipeline-mode", choices=["auto", "real", "sim"], default=None)
    mvp.add_argument("--pipeline-hardware-profile", default=None)
    mvp.add_argument("--pipeline-device", default=None)
    research_partner = subparsers.add_parser(
        "research_partner",
        help="研究伙伴主线：固定 bootstrap，会执行轻量 bootstrap 并跳过 mvp/writeup",
        description="研究伙伴主线：固定 bootstrap，会执行轻量 bootstrap 并跳过 mvp/writeup，用于检索/idea/critique 的合同化入口。",
    )
    _add_common_api_args(research_partner)
    research_partner.add_argument("--experiment", default="paper_writer")
    research_partner.add_argument("--run-dir", default=None)
    research_partner.add_argument("--engine", choices=["semanticscholar", "openalex"], default="openalex")
    research_partner.add_argument("--idea-name", default="paper_writer_research_partner")
    research_partner.add_argument("--title", default="Evidence-first Research Partner Session")
    research_partner.add_argument(
        "--description",
        default="Bootstrap an evidence-first workspace for search, idea generation, and critique.",
    )
    research_partner.add_argument("--literature-top-k", type=int, default=5)
    research_partner.add_argument(
        "--rubric-profile",
        choices=["default", "cvpr", "journal_q2"],
        default="default",
    )
    research_partner.add_argument(
        "--evidence-file",
        action="append",
        default=[],
    )

    writeup = subparsers.add_parser(
        "writeup",
        help="独立写作入口：对现有 workspace 或 scientist folder 触发写作/编译。",
    )
    _add_common_api_args(writeup)
    writeup.add_argument("--workflow-kind", choices=["mvp", "scientist"], default="mvp")
    writeup.add_argument("--workspace", default=None)
    writeup.add_argument("--folder", default=None)
    writeup.add_argument("--writeup-model", default="gpt-5.4-xhigh")
    writeup.add_argument("--engine", choices=["semanticscholar", "openalex"], default="openalex")
    writeup.add_argument("--refine-profile", choices=["fast", "balanced", "deep"], default="balanced")
    writeup.add_argument("--gateway-profile", choices=["safe", "full"], default=None)
    writeup.add_argument("--existing-draft", default=None)
    writeup.add_argument("--enforce-disclosure", dest="enforce_disclosure", action="store_true")
    writeup.add_argument("--no-enforce-disclosure", dest="enforce_disclosure", action="store_false")
    writeup.add_argument("--skip-chktex-fix", dest="skip_chktex_fix", action="store_true")
    writeup.add_argument("--no-skip-chktex-fix", dest="skip_chktex_fix", action="store_false")
    writeup.add_argument("--no-writing", action="store_true")
    writeup.set_defaults(enforce_disclosure=None, skip_chktex_fix=None)

    return parser


def _build_entry_cmd(
    args: argparse.Namespace,
) -> Callable[[argparse.Namespace, list[str]], list[str]]:
    if args.entry == "scientist":
        return _build_scientist_cmd
    if args.entry == "mvp":
        return _build_mvp_cmd
    if args.entry == "writeup":
        return _build_writeup_cmd
    if args.entry == "research_partner":
        return _build_research_cmd
    raise ValueError(f"Unsupported entry: {args.entry}")


def _stream_process_output(proc: Any) -> SimpleNamespace:
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    for line in getattr(proc, "stdout", None) or []:
        rendered = str(line)
        stdout_chunks.append(rendered)
        print(_sanitize_embedded_paths(_sanitize_cli_text(rendered)), end="")
    for line in getattr(proc, "stderr", None) or []:
        rendered = str(line)
        stderr_chunks.append(rendered)
        print(_sanitize_embedded_paths(_sanitize_cli_text(rendered)), end="", file=sys.stderr)
    returncode = int(proc.wait())
    return SimpleNamespace(
        returncode=returncode,
        stdout="".join(stdout_chunks),
        stderr="".join(stderr_chunks),
    )


def main() -> None:
    _require_virtualenv("launch_user_entry.py")
    parser = build_parser()
    args, passthrough = parser.parse_known_args()
    cfg = _collect_effective_config(args)

    cmd_builder = _build_entry_cmd(args)
    cmd = cmd_builder(args, passthrough)

    _print_effective_configuration(args=args, cfg=cfg, cmd=cmd, passthrough=passthrough)
    if args.dry_run:
        return

    _validate_effective_config(args, cfg)
    env = _apply_api_env(args, cfg)

    if args.entry == "research_partner":
        child = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        proc = _stream_process_output(child)
    else:
        proc = subprocess.run(cmd, cwd=str(ROOT), env=env, check=False, capture_output=True, text=True)
        if proc.stdout:
            print(_sanitize_embedded_paths(_sanitize_cli_text(proc.stdout)), end="")
        if proc.stderr:
            print(_sanitize_embedded_paths(_sanitize_cli_text(proc.stderr)), end="", file=sys.stderr)
    _maybe_materialize_research_partner(
        entry=args.entry,
        args=args,
        completed=proc,
        materialize_env=_materialization_env(env),
    )
    raise SystemExit(proc.returncode)


if __name__ == "__main__":
    main()
