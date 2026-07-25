from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from engine.secret_redaction import redact_secrets

from .api import load_service
from .models import ExecutionProfile
from .provider import ProviderRegistry, preflight_openai_compatible

_FORBIDDEN_SECRET_OPTIONS = {
    "--api-key",
    "--openai-api-key",
    "--openai-writeup-api-key",
    "--anthropic-api-key",
    "--auth-token",
    "--password",
}


def _emit(payload: Any) -> None:
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    print(redact_secrets(rendered))


def _reject_secret_argv(argv: Sequence[str]) -> None:
    for item in argv:
        option = item.split("=", 1)[0]
        if option in _FORBIDDEN_SECRET_OPTIONS:
            raise SystemExit(
                f"{option} is forbidden; store credentials in the PaperForge user config directory"
            )


def _static_preflight(workspace: Path) -> dict[str, Any]:
    tools = {
        name: shutil.which(name)
        for name in ("pdflatex", "bibtex", "latexmk", "pdftoppm")
    }
    checks = {
        "python_supported": (3, 10) <= sys.version_info[:2] <= (3, 12),
        "workspace_exists": workspace.is_dir(),
        "workspace_writable": workspace.is_dir() and os_access_write(workspace),
        "latex_compiler_available": bool(tools["latexmk"] or tools["pdflatex"]),
        "bibtex_available": bool(tools["bibtex"]),
        "pdf_renderer_available": bool(tools["pdftoppm"]),
    }
    required = (
        "python_supported",
        "workspace_exists",
        "workspace_writable",
    )
    return {
        "status": "CODE_VERIFIED" if all(checks[key] for key in required) else "FAILED",
        "checks": checks,
        "tools": {key: bool(value) for key, value in tools.items()},
    }


def os_access_write(path: Path) -> bool:
    import os

    return os.access(path, os.W_OK)


def _live_provider_preflight(model: str) -> dict[str, Any]:
    registry = ProviderRegistry()
    config = registry.resolve(model, stage="writeup")
    credential = registry.credential(config)
    if not credential:
        return {
            "provider": config.provider,
            "model": config.model,
            "status": "AUTH_BLOCKED",
            "detail": "no credential is configured",
            "response_received": False,
        }
    from openai import OpenAI

    client = OpenAI(
        **registry.openai_client_kwargs(config),
        max_retries=0,
        timeout=20.0,
    )
    return preflight_openai_compatible(config, client=client).to_dict()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="paperforge")
    subparsers = parser.add_subparsers(dest="command", required=True)

    preflight = subparsers.add_parser("preflight")
    preflight.add_argument("--workspace", default=".")
    preflight.add_argument("--model", default="bailu-turing")
    preflight.add_argument("--live-provider", action="store_true")

    run = subparsers.add_parser("run")
    run.add_argument(
        "--profile",
        required=True,
        choices=[profile.value for profile in ExecutionProfile],
    )
    run.add_argument("--workspace", default=".")
    run.add_argument(
        "--legacy-mode",
        choices=["writeup", "research_partner", "mvp", "scientist"],
        default=None,
    )
    run.add_argument("--proposal-id", default=None)
    run.add_argument("--idempotency-key", default=None)

    approve = subparsers.add_parser("approve")
    approve.add_argument("--proposal-id", required=True)
    approve.add_argument("--workspace", default=".")
    approve.add_argument("--scope", choices=["static", "mini", "full"], default="mini")

    resume = subparsers.add_parser("resume")
    resume.add_argument("--workspace", default=".")
    resume.add_argument("--run-id", default=None)

    status = subparsers.add_parser("status")
    status.add_argument("--workspace", default=".")
    status.add_argument("--run-id", default=None)

    publish = subparsers.add_parser("publish")
    publish.add_argument("--workspace", default=".")
    publish.add_argument(
        "--template",
        required=True,
        choices=["generic", "cvpr", "ieee", "elsevier"],
    )
    publish.add_argument("--main-tex", default=None)

    release = subparsers.add_parser("release")
    release.add_argument("--workspace", default=".")
    release.add_argument("--run-id", default=None)

    for alias, profile in (
        ("writeup", ExecutionProfile.WRITING_ONLY.value),
        ("research_partner", ExecutionProfile.RESEARCH.value),
        ("mvp", ExecutionProfile.FULL.value),
        ("scientist", ExecutionProfile.FULL.value),
    ):
        compatibility = subparsers.add_parser(alias)
        compatibility.add_argument("--workspace", default=".")
        compatibility.set_defaults(compatibility_profile=profile, legacy_mode=alias)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    _reject_secret_argv(arguments)
    args = build_parser().parse_args(arguments)

    if args.command == "preflight":
        workspace = Path(args.workspace).expanduser().resolve()
        payload = _static_preflight(workspace)
        if args.live_provider:
            payload["provider"] = _live_provider_preflight(args.model)
        _emit(payload)
        provider_failed = payload.get("provider", {}).get("status") == "FAILED"
        return 1 if payload["status"] == "FAILED" or provider_failed else 0

    if hasattr(args, "compatibility_profile"):
        _, service = load_service(args.workspace, profile=args.compatibility_profile)
        result = service.run(
            profile=args.compatibility_profile,
            legacy_mode=args.legacy_mode,
        )
        _emit(result.to_dict())
        return 0

    _, service = load_service(
        args.workspace,
        profile=getattr(args, "profile", None),
    )
    if args.command == "run":
        result = service.run(
            profile=args.profile,
            legacy_mode=args.legacy_mode,
            proposal_id=args.proposal_id,
            idempotency_key=args.idempotency_key,
        )
        _emit(result.to_dict())
    elif args.command == "approve":
        _emit(
            service.approve(
                args.proposal_id,
                scope={"maximum_stage": args.scope},
            )
        )
    elif args.command == "resume":
        _emit(service.resume(args.run_id).to_dict())
    elif args.command == "status":
        _emit(service.status(args.run_id).to_dict())
    elif args.command == "publish":
        _emit(service.publish(template=args.template, main_tex=args.main_tex))
    elif args.command == "release":
        _emit(service.release(run_id=args.run_id).to_dict())
    else:
        raise AssertionError(args.command)
    return 0
