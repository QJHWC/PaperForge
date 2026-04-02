from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict


WORKSPACE_CONFIG_FILENAME = "workspace_config.json"
DEFAULT_WORKSPACE_CONFIG: Dict[str, Any] = {
    "writeup_model": "gpt-5.4-xhigh",
    "gateway_profile": "safe",
    "existing_draft": None,
    "enforce_disclosure": False,
    "skip_chktex_fix": True,
    "active_source_draft_id": None,
    "active_template_id": None,
    "latest_migration_id": None,
}


def workspace_config_path(workspace: str | Path) -> Path:
    return Path(workspace).expanduser().resolve() / WORKSPACE_CONFIG_FILENAME


def load_workspace_config(workspace: str | Path) -> Dict[str, Any]:
    path = workspace_config_path(workspace)
    if not path.exists():
        return dict(DEFAULT_WORKSPACE_CONFIG)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return dict(DEFAULT_WORKSPACE_CONFIG)
    if not isinstance(payload, dict):
        return dict(DEFAULT_WORKSPACE_CONFIG)
    config = dict(DEFAULT_WORKSPACE_CONFIG)
    config.update({k: v for k, v in payload.items() if k in DEFAULT_WORKSPACE_CONFIG})
    return config


def save_workspace_config(workspace: str | Path, config: Dict[str, Any]) -> Dict[str, Any]:
    path = workspace_config_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(DEFAULT_WORKSPACE_CONFIG)
    payload.update({k: v for k, v in config.items() if k in DEFAULT_WORKSPACE_CONFIG})
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def resolve_config_path(workspace: str | Path, path_or_none: str | None) -> str | None:
    if path_or_none is None:
        return None
    candidate = Path(path_or_none).expanduser()
    if candidate.is_absolute():
        return str(candidate.resolve())
    return str((Path(workspace).expanduser().resolve() / candidate).resolve())
