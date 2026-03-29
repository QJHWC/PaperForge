from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence


ROOT = Path(__file__).resolve().parent.parent
AGENT_SCHEMA_DIR = ROOT / "agents" / "schemas"


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load_schema(name: str) -> Dict[str, Any]:
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
    return " ".join(command)


def existing_artifacts(paths: Iterable[Path | None]) -> List[Dict[str, Any]]:
    artifacts: List[Dict[str, Any]] = []
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
    input_schema: Dict[str, Any]
    input: Dict[str, Any]
    command: List[str] = field(default_factory=list)
    workspace: str | None = None
    trace: List[Dict[str, Any]] = field(default_factory=list)
    artifacts: List[Dict[str, Any]] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    returncode: int | None = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def execute_command(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=str(cwd or ROOT),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def default_python_command(script_path: str, *extra: str) -> List[str]:
    return [sys.executable, str(ROOT / script_path), *extra]


def append_trace(trace: List[Dict[str, Any]], stage: str, status: str, detail: str) -> None:
    trace.append(asdict(TraceEvent(stage=stage, status=status, detail=detail)))


def env_with_optional_system_python(base: Dict[str, str] | None = None) -> Dict[str, str]:
    env = dict(base or os.environ)
    env.setdefault("PAPERFORGE_ALLOW_SYSTEM_PYTHON", "1")
    return env
