from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from engine.secret_redaction import (
    redact_command,
    redact_secrets,
    redact_structure,
    secret_values_from_env,
)
from paperforge.models import ExecutionProfile
from paperforge.policy import Action, ExecutionPolicy, PolicyViolation

ROOT = Path(__file__).resolve().parent.parent
AGENT_SCHEMA_DIR = ROOT / "agents" / "schemas"
_LOCAL_PROCESS_ACTIONS = frozenset({Action.LOCAL_EXECUTE})


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load_schema(name: str) -> dict[str, Any]:
    path = AGENT_SCHEMA_DIR / f"{name}.schema.json"
    return json.loads(path.read_text(encoding="utf-8"))


def coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def normalize_path(path_or_none: str | None) -> Path | None:
    if not path_or_none:
        return None
    path = Path(path_or_none).expanduser()
    if not path.is_absolute():
        path = (ROOT / path).resolve()
    return path.resolve()


def planned_command(command: Sequence[str]) -> str:
    return " ".join(redact_command(command))


def existing_artifacts(paths: Iterable[Path | None]) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in paths:
        if path is None:
            continue
        resolved = path.expanduser().resolve()
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        artifacts.append(
            {
                "path": key,
                "exists": resolved.exists(),
                "kind": "directory" if resolved.is_dir() else "file",
            }
        )
    return artifacts


@dataclass
class TraceEvent:
    stage: str
    status: str
    detail: str
    timestamp: str = field(default_factory=now_iso)


@dataclass
class AgentBridgeResult:
    agent: str
    entrypoint: str
    status: str
    input_schema: dict[str, Any]
    input: dict[str, Any]
    command: list[str] = field(default_factory=list)
    workspace: str | None = None
    trace: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    returncode: int | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["command"] = redact_command(self.command)
        return redact_structure(payload)


def _resolve_execution_policy(
    *,
    env: dict[str, str] | None,
    profile: str | ExecutionProfile | None,
    policy: ExecutionPolicy | None,
) -> ExecutionPolicy:
    inherited_profile = os.getenv("PAPERFORGE_EXECUTION_PROFILE")
    child_profile = (env or {}).get("PAPERFORGE_EXECUTION_PROFILE")
    profile_values = [
        ExecutionProfile(value)
        for value in (inherited_profile, child_profile, profile)
        if value is not None
    ]
    if len(set(profile_values)) > 1:
        raise PolicyViolation(
            "conflicting execution profiles for child process"
        )
    requested_profile = profile_values[0] if profile_values else None
    if policy is None:
        if requested_profile is None:
            raise PolicyViolation(
                "child process execution requires an explicit profile"
            )
        return ExecutionPolicy.from_value(requested_profile)

    if requested_profile is not None and requested_profile is not policy.profile:
        raise PolicyViolation(
            "execution profile mismatch between process environment "
            "and policy"
        )
    return policy


def _command_option(argv: Sequence[str], flag: str) -> str | None:
    try:
        index = argv.index(flag)
    except ValueError:
        return None
    return argv[index + 1] if index + 1 < len(argv) else None


def _bound_legacy_action(argv: Sequence[str]) -> Action | None:
    if len(argv) < 2:
        return None
    entrypoint = Path(argv[1]).expanduser()
    if not entrypoint.is_absolute():
        entrypoint = ROOT / entrypoint
    try:
        relative_entrypoint = entrypoint.resolve().relative_to(ROOT)
    except ValueError:
        return None

    if relative_entrypoint.as_posix() == "launch_scientist.py":
        return Action.LOCAL_EXECUTE
    if relative_entrypoint.as_posix() != "launch_mvp_workflow.py":
        return None

    phase = (_command_option(argv, "--phase") or "").strip().lower()
    if phase == "refine":
        return Action.DRAFT_EDIT
    if (
        phase == "bootstrap"
        and "--skip-mvp-run" in argv
        and "--skip-writeup" in argv
    ):
        return Action.PROPOSAL_CREATE
    return Action.LOCAL_EXECUTE


def _resolve_process_action(
    argv: Sequence[str],
    requested: Action | str | None,
) -> Action:
    bound_action = _bound_legacy_action(argv)
    requested_action = Action(requested) if requested is not None else None
    if bound_action is not None:
        if requested_action is not None and requested_action is not bound_action:
            raise PolicyViolation(
                "requested action does not match the fixed legacy adapter"
            )
        return bound_action

    resolved = requested_action or Action.LOCAL_EXECUTE
    if resolved not in _LOCAL_PROCESS_ACTIONS:
        raise PolicyViolation(
            "unbound subprocess cannot claim a non-process action"
        )
    return resolved


def execute_command(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    action: Action | str | None = None,
    profile: str | ExecutionProfile | None = None,
    policy: ExecutionPolicy | None = None,
) -> subprocess.CompletedProcess[str]:
    argv = [str(part) for part in command]
    if not argv:
        raise ValueError("command must not be empty")

    process_secret_values = secret_values_from_env(env)
    safe_argv = redact_command(
        argv,
        secret_values=process_secret_values,
    )
    resolved_action = _resolve_process_action(argv, action)
    resolved_policy = _resolve_execution_policy(
        env=env,
        profile=profile,
        policy=policy,
    )
    resolved_policy.validate_command(safe_argv, resolved_action)

    completed = subprocess.run(
        argv,
        cwd=str(cwd or ROOT),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    return subprocess.CompletedProcess(
        args=safe_argv,
        returncode=completed.returncode,
        stdout=redact_secrets(
            completed.stdout or "",
            secret_values=process_secret_values,
        ),
        stderr=redact_secrets(
            completed.stderr or "",
            secret_values=process_secret_values,
        ),
    )


def default_python_command(script_path: str, *extra: str) -> list[str]:
    return [sys.executable, str(ROOT / script_path), *extra]


def append_trace(
    trace: list[dict[str, Any]],
    stage: str,
    status: str,
    detail: str,
) -> None:
    trace.append(
        asdict(
            TraceEvent(
                stage=redact_secrets(stage),
                status=redact_secrets(status),
                detail=redact_secrets(detail),
            )
        )
    )


def env_with_optional_system_python(
    base: dict[str, str] | None = None,
) -> dict[str, str]:
    env = dict(base or os.environ)
    env.setdefault("PAPERFORGE_ALLOW_SYSTEM_PYTHON", "1")
    return env
