from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import ExecutionProfile


class ConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class PaperForgeConfig:
    workspace: Path
    profile: ExecutionProfile = ExecutionProfile.WRITING_ONLY
    provider: str = "bailu"
    model: str = "bailu-turing"
    generation_profile: str = "safe"

    @classmethod
    def load(
        cls,
        workspace: str | Path,
        *,
        profile: str | ExecutionProfile | None = None,
        env: Mapping[str, str] | None = None,
        config_dir: Path | None = None,
    ) -> PaperForgeConfig:
        environment = dict(env or os.environ)
        user_config_dir = config_dir or (
            Path(environment.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "paperforge"
        )
        payload: dict[str, Any] = {}
        config_file = user_config_dir / "config.json"
        if config_file.exists():
            try:
                loaded = json.loads(config_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ConfigurationError("PaperForge user config is unreadable") from exc
            if not isinstance(loaded, dict):
                raise ConfigurationError("PaperForge user config must be a JSON object")
            payload = loaded

        resolved_workspace = Path(workspace).expanduser().resolve()
        selected_profile = ExecutionProfile(
            profile
            or environment.get("PAPERFORGE_EXECUTION_PROFILE")
            or payload.get("profile")
            or ExecutionProfile.WRITING_ONLY.value
        )
        generation_profile = str(
            environment.get("PAPERFORGE_GENERATION_PROFILE")
            or payload.get("generation_profile")
            or "safe"
        )
        if generation_profile not in {"safe", "full"}:
            raise ConfigurationError("generation_profile must be safe or full")
        return cls(
            workspace=resolved_workspace,
            profile=selected_profile,
            provider=str(payload.get("provider") or "bailu"),
            model=str(payload.get("model") or "bailu-turing"),
            generation_profile=generation_profile,
        )

    def public_dict(self) -> dict[str, str]:
        return {
            "workspace": str(self.workspace),
            "profile": self.profile.value,
            "provider": self.provider,
            "model": self.model,
            "generation_profile": self.generation_profile,
        }
