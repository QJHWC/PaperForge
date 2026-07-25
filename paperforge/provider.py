from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from engine.secret_redaction import redact_secrets


class ProviderConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class ProviderCapabilities:
    stream: bool = True
    unsupported_request_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProviderConfig:
    provider: str
    protocol: str
    base_url: str | None
    model: str
    credential_alias: str
    capabilities: ProviderCapabilities = field(default_factory=ProviderCapabilities)
    max_tokens: int = 4096

    def public_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["base_url"] = sanitize_url(self.base_url)
        return payload


@dataclass(frozen=True)
class ProviderPreflightReport:
    provider: str
    model: str
    status: str
    detail: str
    response_received: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def sanitize_url(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path.rstrip("/"), "", ""))


def _first_non_empty(env: Mapping[str, str], names: tuple[str, ...]) -> str | None:
    for name in names:
        value = str(env.get(name, "")).strip()
        if value:
            return value
    return None


def _unique_non_empty(env: Mapping[str, str], names: tuple[str, ...], label: str) -> str | None:
    found = [(name, str(env.get(name, "")).strip()) for name in names if str(env.get(name, "")).strip()]
    distinct = {value.rstrip("/") for _, value in found}
    if len(distinct) > 1:
        sources = ", ".join(name for name, _ in found)
        raise ProviderConfigurationError(f"conflicting {label} values: {sources}")
    return found[0][1] if found else None


class CredentialResolver:
    def __init__(
        self,
        *,
        env: Mapping[str, str] | None = None,
        config_dir: Path | None = None,
    ) -> None:
        self.env = dict(env or os.environ)
        self.config_dir = config_dir or (
            Path(self.env.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "paperforge"
        )

    def resolve(self, alias: str, *, legacy_names: tuple[str, ...] = ()) -> str | None:
        canonical_name = f"PAPERFORGE_CREDENTIAL_{alias.upper().replace('-', '_')}"
        canonical = str(self.env.get(canonical_name, "")).strip()
        legacy = _unique_non_empty(self.env, legacy_names, f"credential alias {alias}")
        if canonical and legacy and canonical != legacy:
            raise ProviderConfigurationError(
                f"{canonical_name} conflicts with legacy credential variables"
            )
        if canonical:
            return canonical
        if legacy:
            return legacy

        credentials_file = self.config_dir / "credentials.json"
        if not credentials_file.exists():
            return None
        mode = credentials_file.stat().st_mode & 0o777
        if os.name != "nt" and mode & 0o077:
            raise ProviderConfigurationError(
                f"credential file permissions must be 0600: {credentials_file}"
            )
        try:
            payload = json.loads(credentials_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProviderConfigurationError("credential file is unreadable") from exc
        value = str(payload.get(alias, "")).strip() if isinstance(payload, dict) else ""
        return value or None


class ProviderRegistry:
    def __init__(self, *, env: Mapping[str, str] | None = None) -> None:
        self.env = dict(env or os.environ)

    def resolve(self, model: str, *, stage: str = "default") -> ProviderConfig:
        if model == "bailu-turing":
            base_url = _unique_non_empty(
                self.env,
                ("OPENAI_BASE_URL", "OPENAI_WRITEUP_BASE_URL", "OPENAI_API_BASE"),
                "OpenAI-compatible base URL",
            )
            return ProviderConfig(
                provider="bailu",
                protocol="openai-compatible",
                base_url=base_url or "https://bailucode.com/openapi/v1",
                model=model,
                credential_alias="bailu_primary",
                capabilities=ProviderCapabilities(
                    stream=False,
                    unsupported_request_fields=("reasoning_effort", "seed", "n", "stop"),
                ),
                max_tokens=4096,
            )

        base_url = _unique_non_empty(
            self.env,
            ("OPENAI_BASE_URL", "OPENAI_WRITEUP_BASE_URL", "OPENAI_API_BASE"),
            "OpenAI-compatible base URL",
        )
        credential_alias = "openai_writeup" if stage == "writeup" else "openai_primary"
        return ProviderConfig(
            provider="openai",
            protocol="openai",
            base_url=base_url,
            model=model,
            credential_alias=credential_alias,
        )

    def credential(self, config: ProviderConfig) -> str | None:
        resolver = CredentialResolver(env=self.env)
        if config.provider == "bailu":
            return resolver.resolve(
                config.credential_alias,
                legacy_names=("OPENAI_API_KEY", "OPENAI_WRITEUP_API_KEY"),
            )
        if config.credential_alias == "openai_writeup":
            writeup = resolver.resolve(
                config.credential_alias,
                legacy_names=("OPENAI_WRITEUP_API_KEY",),
            )
            return writeup or resolver.resolve(
                "openai_primary",
                legacy_names=("OPENAI_API_KEY",),
            )
        return resolver.resolve(
            config.credential_alias,
            legacy_names=("OPENAI_API_KEY",),
        )

    @staticmethod
    def filter_payload(config: ProviderConfig, payload: Mapping[str, Any]) -> dict[str, Any]:
        filtered = {
            key: value
            for key, value in payload.items()
            if key not in set(config.capabilities.unsupported_request_fields)
        }
        if not config.capabilities.stream:
            filtered["stream"] = False
        return filtered

    def openai_client_kwargs(self, config: ProviderConfig) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        credential = self.credential(config)
        if credential:
            kwargs["api_key"] = credential
        if config.base_url:
            kwargs["base_url"] = config.base_url.rstrip("/")
        return kwargs


class ProviderRequestBuilder:
    """Single constructor for OpenAI-compatible chat request payloads."""

    def __init__(self, registry: ProviderRegistry | None = None) -> None:
        self.registry = registry or ProviderRegistry()

    @staticmethod
    def build(
        config: ProviderConfig,
        *,
        messages: Any,
        request_model: str | None = None,
        **parameters: Any,
    ) -> dict[str, Any]:
        if not isinstance(messages, list | tuple) or not messages:
            raise ProviderConfigurationError("chat messages must be a non-empty sequence")
        payload = {
            "model": request_model or config.model,
            "messages": list(messages),
            **parameters,
        }
        return ProviderRegistry.filter_payload(config, payload)

    def chat_completion(
        self,
        model: str,
        *,
        messages: Any,
        stage: str = "default",
        request_model: str | None = None,
        **parameters: Any,
    ) -> dict[str, Any]:
        config = self.registry.resolve(model, stage=stage)
        return self.build(
            config,
            messages=messages,
            request_model=request_model,
            **parameters,
        )


def create_chat_completion(
    client: Any,
    model: str,
    *,
    messages: Any,
    stage: str = "default",
    request_model: str | None = None,
    **parameters: Any,
) -> Any:
    payload = ProviderRequestBuilder().chat_completion(
        model,
        messages=messages,
        stage=stage,
        request_model=request_model,
        **parameters,
    )
    return client.chat.completions.create(**payload)


def build_aider_model(
    model_name: str,
    *,
    generation_profile: str = "safe",
    stage: str = "writeup",
) -> Any:
    """Build every Aider model through the same provider and credential resolver."""
    from aider.models import Model

    route_claude_via_openai = os.getenv(
        "PAPERFORGE_CLAUDE_OPENAI_COMPAT", "0"
    ).strip().lower() in {"1", "true", "yes", "on"}
    if model_name == "deepseek-coder-v2-0724":
        aider_name = "deepseek/deepseek-coder"
    elif model_name.startswith("deepseek-"):
        aider_name = f"deepseek/{model_name}"
    elif model_name.startswith("claude-") and not route_claude_via_openai:
        aider_name = f"anthropic/{model_name}"
    elif model_name.startswith(("gpt-", "o1", "o3")) or model_name == "bailu-turing" or model_name.startswith("claude-") and route_claude_via_openai:
        aider_name = f"openai/{model_name}"
    elif model_name == "llama3.1-405b":
        aider_name = "openrouter/meta-llama/llama-3.1-405b-instruct"
    else:
        aider_name = model_name

    model = Model(aider_name)
    if not getattr(model, "extra_params", None):
        model.extra_params = {}

    if aider_name.startswith("openai/"):
        registry = ProviderRegistry()
        config = registry.resolve(model_name, stage=stage)
        kwargs = registry.openai_client_kwargs(config)
        if kwargs.get("api_key"):
            model.extra_params["api_key"] = kwargs["api_key"]
        if kwargs.get("base_url"):
            model.extra_params["api_base"] = kwargs["base_url"]
        model.extra_params["max_tokens"] = config.max_tokens
        if "reasoning_effort" not in config.capabilities.unsupported_request_fields:
            effort = "high" if generation_profile == "full" else "low"
            model.extra_params["reasoning_effort"] = effort
        headers = dict(model.extra_params.get("extra_headers", {}))
        headers.setdefault("User-Agent", os.getenv("OPENAI_USER_AGENT", "PaperForge/3.0"))
        model.extra_params["extra_headers"] = headers
    return model


def preflight_openai_compatible(
    config: ProviderConfig,
    *,
    client: Any,
) -> ProviderPreflightReport:
    payload = ProviderRequestBuilder.build(
        config,
        messages=[{"role": "user", "content": "Reply with OK."}],
        max_tokens=16,
        temperature=0,
        stream=False,
    )
    try:
        response = client.chat.completions.create(**payload)
        choices = getattr(response, "choices", None) or []
        content = getattr(getattr(choices[0], "message", None), "content", None) if choices else None
        if not isinstance(content, str) or not content.strip():
            return ProviderPreflightReport(
                provider=config.provider,
                model=config.model,
                status="FAILED",
                detail="chat completion returned no message content",
            )
        return ProviderPreflightReport(
            provider=config.provider,
            model=config.model,
            status="EXTERNAL_SERVICE_VERIFIED",
            detail="minimal chat completion succeeded",
            response_received=True,
        )
    except Exception as exc:
        status_code = getattr(exc, "status_code", None)
        error_text = redact_secrets(str(exc))
        if status_code in {401, 403} or "401" in error_text or "403" in error_text:
            return ProviderPreflightReport(
                provider=config.provider,
                model=config.model,
                status="AUTH_BLOCKED",
                detail="provider rejected the configured credential",
            )
        return ProviderPreflightReport(
            provider=config.provider,
            model=config.model,
            status="FAILED",
            detail=f"{exc.__class__.__name__}: {error_text.splitlines()[0][:240]}",
        )
