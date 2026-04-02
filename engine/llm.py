import json
import os
import re
from typing import Any, Dict

import anthropic
import backoff
import openai

DEFAULT_MAX_NUM_TOKENS = 4096
DEFAULT_GATEWAY_PROFILE = "safe"
GATEWAY_PROFILES: Dict[str, Dict[str, Any]] = {
    "safe": {
        "stream": False,
        "max_tokens": 4096,
        "reasoning_effort": "low",
    },
    "full": {
        "stream": True,
        "max_tokens": 16384,
        "reasoning_effort": "high",
    },
}
ANTHROPIC_PROMPT_CACHE_MAX_BREAKPOINTS = 4
ANTHROPIC_PROMPT_CACHE_DEFAULT_SYSTEM_MIN_CHARS = 512
ANTHROPIC_PROMPT_CACHE_DEFAULT_USER_MIN_CHARS = 4096
_ANTHROPIC_PROMPT_CACHE_RUNTIME_ENABLED = True

AVAILABLE_LLMS = [
    # Anthropic models
    "claude-3-5-sonnet-20240620",
    "claude-3-5-sonnet-20241022",
    "claude-sonnet-4-6",
    "claude-sonnet-4-5-20250929",
    "claude-4.6-sonnet",
    "claude-4.6-opus",
    "claude-opus-4.6",
    "claude-opus-4-6",
    # OpenAI models
    "gpt-5.4-xhigh",
    "gpt-5.4-xhigh-superfast",
    "gpt-5.2-xhigh",
    "gpt-5.3",
    "gpt-5.3-codex",
    "gpt-5.3-codex-xhigh",
    "gpt-5.3-codex xhigh",
    "gpt-4o-mini",
    "gpt-4o-mini-2024-07-18",
    "gpt-4o",
    "gpt-4o-2024-05-13",
    "gpt-4o-2024-08-06",
    "gpt-4.1",
    "gpt-4.1-2025-04-14",
    "gpt-4.1-mini",
    "gpt-4.1-mini-2025-04-14",
    "gpt-4.1-nano",
    "gpt-4.1-nano-2025-04-14",
    "o1",
    "o1-2024-12-17",
    "o1-preview-2024-09-12",
    "o1-mini",
    "o1-mini-2024-09-12",
    "o3-mini",
    "o3-mini-2025-01-31",
    # xAI Grok models (OpenAI-compatible endpoint)
    "grok-4.1-thinking",
    # OpenRouter models
    "llama3.1-405b",
    # Anthropic Claude models via Amazon Bedrock
    "bedrock/anthropic.claude-3-sonnet-20240229-v1:0",
    "bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0",
    "bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0",
    "bedrock/anthropic.claude-3-haiku-20240307-v1:0",
    "bedrock/anthropic.claude-3-opus-20240229-v1:0",
    # Anthropic Claude models Vertex AI
    "vertex_ai/claude-3-opus@20240229",
    "vertex_ai/claude-3-5-sonnet@20240620",
    "vertex_ai/claude-3-5-sonnet-v2@20241022",
    "vertex_ai/claude-3-sonnet@20240229",
    "vertex_ai/claude-3-haiku@20240307",
    # DeepSeek models
    "deepseek-chat",
    "deepseek-coder",
    "deepseek-reasoner",
    # Google Gemini models
    "gemini-3.1-pro-preview",
    "gemini-3-pro-preview",
    "gemini-1.5-flash",
    "gemini-1.5-pro",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
    "gemini-2.0-flash-thinking-exp-01-21",
    "gemini-2.5-pro-preview-03-25",
    "gemini-2.5-pro-exp-03-25",
]

CLAUDE_MODEL_ALIASES = {
    "claude-4.6-sonnet": "claude-sonnet-4-6",
    "claude-sonnet-4.6": "claude-sonnet-4-6",
    # Normalize Opus aliases to a single canonical model id.
    "claude-4.6-opus": "claude-opus-4-6",
    "claude-opus-4.6": "claude-opus-4-6",
}


def normalize_claude_model_name(model: str) -> str:
    normalized = CLAUDE_MODEL_ALIASES.get(model, model)
    # Optional compatibility switch for gateways that expose only sonnet.
    if os.getenv("PAPERFORGE_FORCE_CLAUDE_OPUS_TO_SONNET", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        if normalized == "claude-opus-4-6":
            return "claude-sonnet-4-6"
    return normalized


def fallback_claude_model_name(model: str):
    if normalize_claude_model_name(model) == "claude-opus-4-6":
        return "claude-sonnet-4-6"
    return None


def _is_truthy_env(var_name: str, default: str = "0") -> bool:
    return os.getenv(var_name, default).strip().lower() in {"1", "true", "yes", "on"}


def _env_positive_int(var_name: str, default: int) -> int:
    raw = os.getenv(var_name, str(default)).strip()
    try:
        parsed = int(raw)
        if parsed > 0:
            return parsed
    except ValueError:
        pass
    return default


def normalize_gateway_profile_name(profile: str | None) -> str:
    candidate = str(profile or os.getenv("PAPERFORGE_GATEWAY_PROFILE", "")).strip().lower()
    if candidate in GATEWAY_PROFILES:
        return candidate
    return DEFAULT_GATEWAY_PROFILE


def resolve_gateway_profile(profile: str | None = None) -> Dict[str, Any]:
    name = normalize_gateway_profile_name(profile)
    resolved = {"name": name, **GATEWAY_PROFILES[name]}

    max_tokens_raw = os.getenv("PAPERFORGE_OPENAI_COMPAT_MAX_TOKENS", "").strip()
    if max_tokens_raw:
        try:
            parsed = int(max_tokens_raw)
            if parsed > 0:
                resolved["max_tokens"] = parsed
        except ValueError:
            pass

    reasoning_effort = os.getenv("OPENAI_REASONING_EFFORT", "").strip()
    if reasoning_effort:
        resolved["reasoning_effort"] = reasoning_effort

    if "PAPERFORGE_AIDER_STREAM" in os.environ:
        resolved["stream"] = _is_truthy_env("PAPERFORGE_AIDER_STREAM", "0")

    return resolved


def gateway_profile_env_overrides(profile: str | None = None) -> Dict[str, str]:
    resolved = resolve_gateway_profile(profile)
    return {
        "PAPERFORGE_GATEWAY_PROFILE": resolved["name"],
        "PAPERFORGE_AIDER_STREAM": "1" if resolved["stream"] else "0",
        "PAPERFORGE_OPENAI_COMPAT_MAX_TOKENS": str(resolved["max_tokens"]),
        "OPENAI_REASONING_EFFORT": str(resolved["reasoning_effort"]),
    }


def _max_num_tokens() -> int:
    return int(resolve_gateway_profile()["max_tokens"])


def _resolve_openai_model_and_reasoning(model: str):
    """
    Allow user-facing aliases like gpt-5.3-codex-xhigh while keeping
    OpenAI-compatible payloads explicit and deterministic.
    """
    request_model = model
    reasoning_effort = os.getenv("OPENAI_REASONING_EFFORT", "").strip()
    if not reasoning_effort:
        reasoning_effort = str(resolve_gateway_profile()["reasoning_effort"]).strip()

    if model in {"gpt-5.3-codex-xhigh", "gpt-5.3-codex xhigh"}:
        request_model = "gpt-5.3-codex"
        if not reasoning_effort:
            reasoning_effort = os.getenv("OPENAI_REASONING_EFFORT_XHIGH", "xhigh").strip()
            if not reasoning_effort:
                reasoning_effort = "xhigh"

    extra_kwargs = {}
    if reasoning_effort:
        extra_kwargs["reasoning_effort"] = reasoning_effort

    return request_model, extra_kwargs


def _default_openai_headers():
    headers = {
        "User-Agent": os.getenv("OPENAI_USER_AGENT", "curl/8.7.1"),
    }
    anthropic_beta = os.getenv("PAPERFORGE_ANTHROPIC_BETA", "").strip()
    if anthropic_beta:
        headers["anthropic-beta"] = anthropic_beta
    return headers


def _route_claude_via_openai() -> bool:
    return _is_truthy_env("PAPERFORGE_CLAUDE_OPENAI_COMPAT", "0")


def _anthropic_prompt_cache_enabled() -> bool:
    if _route_claude_via_openai():
        return False
    if not _is_truthy_env("PAPERFORGE_ANTHROPIC_PROMPT_CACHE", "1"):
        return False
    return _ANTHROPIC_PROMPT_CACHE_RUNTIME_ENABLED


def _anthropic_prompt_cache_disable(exc: Exception) -> None:
    global _ANTHROPIC_PROMPT_CACHE_RUNTIME_ENABLED
    if not _ANTHROPIC_PROMPT_CACHE_RUNTIME_ENABLED:
        return
    _ANTHROPIC_PROMPT_CACHE_RUNTIME_ENABLED = False
    print(
        "Anthropic prompt caching disabled for this process "
        f"after API rejection: {exc}"
    )


def _anthropic_prompt_cache_breakpoint_enabled(text: str, *, min_chars: int) -> bool:
    return isinstance(text, str) and len(text) >= min_chars


def _anthropic_text_block(text: str, *, cache: bool = False) -> dict:
    block = {"type": "text", "text": text}
    if cache:
        block["cache_control"] = {"type": "ephemeral"}
    return block


def _strip_cache_control_from_content(content):
    if not isinstance(content, list):
        return content
    cleaned = []
    for item in content:
        if isinstance(item, dict):
            stripped = {k: v for k, v in item.items() if k != "cache_control"}
            cleaned.append(stripped)
        else:
            cleaned.append(item)
    return cleaned


def _strip_cache_control_from_messages(messages):
    cleaned = []
    for message in messages:
        if not isinstance(message, dict):
            cleaned.append(message)
            continue
        cloned = dict(message)
        cloned["content"] = _strip_cache_control_from_content(message.get("content"))
        cleaned.append(cloned)
    return cleaned


def _strip_cache_control_from_system(system_payload):
    if isinstance(system_payload, list):
        return _strip_cache_control_from_content(system_payload)
    return system_payload


def _is_prompt_cache_unsupported_error(exc: Exception) -> bool:
    text = str(exc).lower()
    markers = ("cache_control", "prompt cache", "prompt-caching", "ephemeral")
    if isinstance(exc, anthropic.APIStatusError):
        status = getattr(exc, "status_code", None)
        if status in {400, 404, 422} and any(marker in text for marker in markers):
            return True
    return any(marker in text for marker in markers)


def _normalize_openai_base_url(base_url: str) -> str:
    cleaned = base_url.strip().rstrip("/")
    if cleaned.endswith("/v1"):
        return cleaned
    return f"{cleaned}/v1"


def _first_non_empty_env(*names):
    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    return None


def _resolve_claude_openai_client_kwargs():
    kwargs = {
        "default_headers": _default_openai_headers(),
    }
    api_key = ***redacted***
        "OPENAI_WRITEUP_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
    )
    base_url = _first_non_empty_env(
        "OPENAI_API_BASE",
        "OPENAI_WRITEUP_BASE_URL",
        "OPENAI_BASE_URL",
        "ANTHROPIC_BASE_URL",
    )

    if api_key:
        ***redacted***"api_key"] = api_key
    if base_url:
        kwargs["base_url"] = _normalize_openai_base_url(base_url)
    return kwargs


def _resolve_openai_client_kwargs(model: str):
    """
    Resolve OpenAI-compatible credentials/base_url with optional model-specific routing.
    """
    kwargs = {}
    if model == "gpt-5.2-xhigh":
        # Dedicated writeup endpoint/key, fallback to default OPENAI_* if unset.
        api_key = ***redacted***"OPENAI_WRITEUP_API_KEY", "OPENAI_API_KEY")
        base_url = _first_non_empty_env(
            "OPENAI_API_BASE",
            "OPENAI_WRITEUP_BASE_URL",
            "OPENAI_BASE_URL",
        )
    else:
        api_key = ***redacted***"OPENAI_API_KEY", "OPENAI_WRITEUP_API_KEY")
        base_url = _first_non_empty_env(
            "OPENAI_API_BASE",
            "OPENAI_BASE_URL",
            "OPENAI_WRITEUP_BASE_URL",
        )

    if api_key:
        ***redacted***"api_key"] = api_key
    if base_url:
        kwargs["base_url"] = _normalize_openai_base_url(base_url)
    kwargs["default_headers"] = _default_openai_headers()
    return kwargs


def _build_anthropic_client(
        *,
        base_url_env: str,
        api_key_env: str,
        auth_token_env: str,
        fallback_api_key_env: str = None,
        fallback_auth_token_env: str = None,
):
    kwargs = {}
    base_url = os.getenv(base_url_env)
    if base_url:
        kwargs["base_url"] = base_url

    if os.getenv(api_key_env):
        kwargs["api_key"] = os.environ[api_key_env]
    elif fallback_api_key_env and os.getenv(fallback_api_key_env):
        kwargs["api_key"] = os.environ[fallback_api_key_env]

    if os.getenv(auth_token_env):
        kwargs["auth_token"] = os.environ[auth_token_env]
    elif fallback_auth_token_env and os.getenv(fallback_auth_token_env):
        kwargs["auth_token"] = os.environ[fallback_auth_token_env]

    return anthropic.Anthropic(**kwargs), base_url


def _should_failover_anthropic(exc: Exception) -> bool:
    # Fail over on quota/rate-limit/auth/connection/server class failures.
    if isinstance(
            exc,
            (
                    anthropic.RateLimitError,
                    anthropic.APITimeoutError,
                    anthropic.APIConnectionError,
                    anthropic.InternalServerError,
                    anthropic.AuthenticationError,
                    anthropic.PermissionDeniedError,
            ),
    ):
        return True

    if isinstance(exc, anthropic.APIStatusError):
        status = getattr(exc, "status_code", None)
        if status in {401, 402, 403, 408, 409, 429, 500, 502, 503, 504}:
            return True

    # Some gateways may return balance/quota errors with non-standard status codes.
    msg = str(exc).lower()
    failover_keywords = [
        "insufficient",
        "balance",
        "quota",
        "credit",
        "rate limit",
        "too many requests",
        "overloaded",
        "temporarily unavailable",
    ]
    return any(k in msg for k in failover_keywords)


class _AnthropicEndpoint:
    def __init__(self, name, messages, model_override=None):
        self.name = name
        self.messages = messages
        self.model_override = (
            normalize_claude_model_name(model_override) if model_override else None
        )


class _AnthropicFailoverMessages:
    def __init__(self, endpoints, primary_model, fallback_model=None):
        self._endpoints = endpoints
        self._active_endpoint_idx = 0
        self._primary_model = normalize_claude_model_name(primary_model)
        self._fallback_model = (
            normalize_claude_model_name(fallback_model) if fallback_model else None
        )
        self._use_fallback_model = False

    def _opus_retry_rounds(self):
        raw = os.getenv("ANTHROPIC_OPUS_RETRY_ROUNDS", "3")
        try:
            return max(1, int(raw))
        except ValueError:
            return 3

    def _prepare_kwargs(self, kwargs, endpoint, target_model):
        call_kwargs = dict(kwargs)
        normalized_model = normalize_claude_model_name(target_model)
        if (
            normalized_model == self._primary_model
            and endpoint.model_override is not None
        ):
            call_kwargs["model"] = endpoint.model_override
        else:
            call_kwargs["model"] = normalized_model
        return call_kwargs

    def _try_endpoints(self, *args, kwargs, target_model, start_idx):
        last_exc = None
        for idx in range(start_idx, len(self._endpoints)):
            endpoint = self._endpoints[idx]
            call_kwargs = self._prepare_kwargs(kwargs, endpoint, target_model)
            try:
                response = endpoint.messages.create(*args, **call_kwargs)
                if idx != self._active_endpoint_idx:
                    print(
                        "Switching Anthropic endpoint to "
                        f"{endpoint.name} (model {call_kwargs['model']})."
                    )
                self._active_endpoint_idx = idx
                return response
            except Exception as exc:
                last_exc = exc
                if not _should_failover_anthropic(exc):
                    raise
                print(
                    "Anthropic request failed on "
                    f"{endpoint.name} with model {call_kwargs['model']}. "
                    "Trying next endpoint."
                )

        if last_exc is not None:
            raise last_exc
        raise RuntimeError("No Anthropic endpoints available.")

    def create(self, *args, **kwargs):
        requested_model = normalize_claude_model_name(
            kwargs.get("model", self._primary_model)
        )
        primary_rounds = 1
        if (
            requested_model == self._primary_model
            and self._fallback_model is not None
            and not self._use_fallback_model
            and requested_model == "claude-opus-4-6"
        ):
            primary_rounds = self._opus_retry_rounds()

        last_exc = None
        for attempt in range(primary_rounds):
            try:
                return self._try_endpoints(
                    *args,
                    kwargs=kwargs,
                    target_model=requested_model,
                    start_idx=self._active_endpoint_idx if attempt == 0 else 0,
                )
            except Exception as exc:
                last_exc = exc
                if attempt < primary_rounds - 1:
                    print(
                        f"Anthropic Opus retry {attempt + 2}/{primary_rounds} "
                        "before Sonnet fallback."
                    )

        if (
            self._fallback_model is not None
            and not self._use_fallback_model
            and requested_model == self._primary_model
        ):
            self._use_fallback_model = True
            print(
                f"Anthropic model fallback enabled: "
                f"{self._primary_model} -> {self._fallback_model}."
            )
            return self._try_endpoints(
                *args,
                kwargs=kwargs,
                target_model=self._fallback_model,
                start_idx=0,
            )

        if last_exc is not None:
            raise last_exc
        raise RuntimeError("Anthropic request failed without explicit exception.")


class _AnthropicFailoverClient:
    def __init__(
            self,
            primary_client,
            opus_client,
            backup_client,
            primary_model,
            fallback_model=None,
            opus_model_override=None,
            backup_model_override=None,
    ):
        endpoints = [_AnthropicEndpoint("primary", primary_client.messages)]
        if opus_client is not None:
            endpoints.append(
                _AnthropicEndpoint(
                    "opus-priority-backup",
                    opus_client.messages,
                    model_override=opus_model_override,
                )
            )
        if backup_client is not None:
            endpoints.append(
                _AnthropicEndpoint(
                    "backup",
                    backup_client.messages,
                    model_override=backup_model_override,
                )
            )

        self.messages = _AnthropicFailoverMessages(
            endpoints,
            primary_model=primary_model,
            fallback_model=fallback_model,
        )


# Get N responses from a single message, used for ensembling.
@backoff.on_exception(backoff.expo, (openai.RateLimitError, openai.APITimeoutError))
def get_batch_responses_from_llm(
        msg,
        client,
        model,
        system_message,
        print_debug=False,
        msg_history=None,
        temperature=0.75,
        n_responses=1,
):
    if msg_history is None:
        msg_history = []

    if 'gpt' in model or model.startswith("grok-") or (
            model.startswith("claude-") and _route_claude_via_openai()
    ):
        request_model = model
        extra_kwargs = {}
        if 'gpt' in model:
            request_model, extra_kwargs = _resolve_openai_model_and_reasoning(model)
        new_msg_history = msg_history + [{"role": "user", "content": msg}]
        response = client.chat.completions.create(
            model=request_model,
            messages=[
                {"role": "system", "content": system_message},
                *new_msg_history,
            ],
            temperature=temperature,
            max_tokens=_max_num_tokens(),
            n=n_responses,
            stop=None,
            seed=0,
            **extra_kwargs,
        )
        content = [r.message.content for r in response.choices]
        new_msg_history = [
            new_msg_history + [{"role": "assistant", "content": c}] for c in content
        ]
    elif model == "llama-3-1-405b-instruct":
        new_msg_history = msg_history + [{"role": "user", "content": msg}]
        response = client.chat.completions.create(
            model="meta-llama/llama-3.1-405b-instruct",
            messages=[
                {"role": "system", "content": system_message},
                *new_msg_history,
            ],
            temperature=temperature,
            max_tokens=_max_num_tokens(),
            n=n_responses,
            stop=None,
        )
        content = [r.message.content for r in response.choices]
        new_msg_history = [
            new_msg_history + [{"role": "assistant", "content": c}] for c in content
        ]
    else:
        content, new_msg_history = [], []
        for _ in range(n_responses):
            c, hist = get_response_from_llm(
                msg,
                client,
                model,
                system_message,
                print_debug=False,
                msg_history=None,
                temperature=temperature,
            )
            content.append(c)
            new_msg_history.append(hist)

    if print_debug:
        print()
        print("*" * 20 + " LLM START " + "*" * 20)
        for j, msg in enumerate(new_msg_history[0]):
            print(f'{j}, {msg["role"]}: {msg["content"]}')
        print(content)
        print("*" * 21 + " LLM END " + "*" * 21)
        print()

    return content, new_msg_history


@backoff.on_exception(backoff.expo, (openai.RateLimitError, openai.APITimeoutError))
def get_response_from_llm(
        msg,
        client,
        model,
        system_message,
        print_debug=False,
        msg_history=None,
        temperature=0.75,
):
    if msg_history is None:
        msg_history = []

    if "claude" in model and not _route_claude_via_openai():
        cache_enabled = _anthropic_prompt_cache_enabled()
        system_payload = system_message
        system_min_chars = _env_positive_int(
            "PAPERFORGE_ANTHROPIC_CACHE_SYSTEM_MIN_CHARS",
            ANTHROPIC_PROMPT_CACHE_DEFAULT_SYSTEM_MIN_CHARS,
        )
        user_min_chars = _env_positive_int(
            "PAPERFORGE_ANTHROPIC_CACHE_USER_MIN_CHARS",
            ANTHROPIC_PROMPT_CACHE_DEFAULT_USER_MIN_CHARS,
        )
        user_cache_allowed = _is_truthy_env("PAPERFORGE_ANTHROPIC_CACHE_USER", "1")

        breakpoints_left = ANTHROPIC_PROMPT_CACHE_MAX_BREAKPOINTS
        system_cache = False
        if (
            cache_enabled
            and _anthropic_prompt_cache_breakpoint_enabled(
                system_message,
                min_chars=system_min_chars,
            )
            and breakpoints_left > 0
        ):
            system_payload = [_anthropic_text_block(system_message, cache=True)]
            system_cache = True
            breakpoints_left -= 1

        user_cache = False
        if (
            cache_enabled
            and user_cache_allowed
            and _anthropic_prompt_cache_breakpoint_enabled(msg, min_chars=user_min_chars)
            and breakpoints_left > 0
        ):
            user_cache = True
            breakpoints_left -= 1

        new_msg_history = msg_history + [
            {
                "role": "user",
                "content": [
                    _anthropic_text_block(msg, cache=user_cache),
                ],
            }
        ]

        request_kwargs = {
            "model": model,
            "max_tokens": _max_num_tokens(),
            "temperature": temperature,
            "system": system_payload,
            "messages": new_msg_history,
        }
        try:
            response = client.messages.create(**request_kwargs)
        except Exception as exc:
            if cache_enabled and _is_prompt_cache_unsupported_error(exc):
                _anthropic_prompt_cache_disable(exc)
                new_msg_history = _strip_cache_control_from_messages(new_msg_history)
                system_payload = _strip_cache_control_from_system(system_payload)
                response = client.messages.create(
                    model=model,
                    max_tokens=_max_num_tokens(),
                    temperature=temperature,
                    system=system_payload,
                    messages=new_msg_history,
                )
                if system_cache or user_cache:
                    print("Retried Anthropic request without prompt caching metadata.")
            else:
                raise
        content = response.content[0].text
        new_msg_history = new_msg_history + [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": content,
                    }
                ],
            }
        ]
    elif 'gpt' in model or (model.startswith("claude-") and _route_claude_via_openai()):
        request_model, extra_kwargs = _resolve_openai_model_and_reasoning(model)
        if model.startswith("claude-"):
            request_model, extra_kwargs = model, {}
        new_msg_history = msg_history + [{"role": "user", "content": msg}]
        response = client.chat.completions.create(
            model=request_model,
            messages=[
                {"role": "system", "content": system_message},
                *new_msg_history,
            ],
            temperature=temperature,
            max_tokens=_max_num_tokens(),
            n=1,
            stop=None,
            seed=0,
            **extra_kwargs,
        )
        content = response.choices[0].message.content
        new_msg_history = new_msg_history + [{"role": "assistant", "content": content}]
    elif model.startswith("grok-"):
        new_msg_history = msg_history + [{"role": "user", "content": msg}]
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_message},
                *new_msg_history,
            ],
            temperature=temperature,
            max_tokens=_max_num_tokens(),
            n=1,
            stop=None,
        )
        content = response.choices[0].message.content
        new_msg_history = new_msg_history + [{"role": "assistant", "content": content}]
    elif "o1" in model or "o3" in model:
        new_msg_history = msg_history + [{"role": "user", "content": msg}]
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "user", "content": system_message},
                *new_msg_history,
            ],
            temperature=1,
            max_completion_tokens=_max_num_tokens(),
            n=1,
            seed=0,
        )
        content = response.choices[0].message.content
        new_msg_history = new_msg_history + [{"role": "assistant", "content": content}]
    elif model in ["meta-llama/llama-3.1-405b-instruct", "llama-3-1-405b-instruct"]:
        new_msg_history = msg_history + [{"role": "user", "content": msg}]
        response = client.chat.completions.create(
            model="meta-llama/llama-3.1-405b-instruct",
            messages=[
                {"role": "system", "content": system_message},
                *new_msg_history,
            ],
            temperature=temperature,
            max_tokens=_max_num_tokens(),
            n=1,
            stop=None,
        )
        content = response.choices[0].message.content
        new_msg_history = new_msg_history + [{"role": "assistant", "content": content}]
    elif model in ["deepseek-chat", "deepseek-coder"]:
        new_msg_history = msg_history + [{"role": "user", "content": msg}]
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_message},
                *new_msg_history,
            ],
            temperature=temperature,
            max_tokens=_max_num_tokens(),
            n=1,
            stop=None,
        )
        content = response.choices[0].message.content
        new_msg_history = new_msg_history + [{"role": "assistant", "content": content}]
    elif model in ["deepseek-reasoner"]:
        new_msg_history = msg_history + [{"role": "user", "content": msg}]
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_message},
                *new_msg_history,
            ],
            n=1,
            stop=None,
        )
        content = response.choices[0].message.content
        new_msg_history = new_msg_history + [{"role": "assistant", "content": content}]
    elif "gemini" in model:
        new_msg_history = msg_history + [{"role": "user", "content": msg}]
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_message},
                *new_msg_history,
            ],
            temperature=temperature,
            max_tokens=_max_num_tokens(),
            n=1,
        )
        content = response.choices[0].message.content
        new_msg_history = new_msg_history + [{"role": "assistant", "content": content}]
    else:
        raise ValueError(f"Model {model} not supported.")

    if print_debug:
        print()
        print("*" * 20 + " LLM START " + "*" * 20)
        for j, msg in enumerate(new_msg_history):
            print(f'{j}, {msg["role"]}: {msg["content"]}')
        print(content)
        print("*" * 21 + " LLM END " + "*" * 21)
        print()

    return content, new_msg_history


def extract_json_between_markers(llm_output):
    # Regular expression pattern to find JSON content between ```json and ```
    json_pattern = r"```json(.*?)```"
    matches = re.findall(json_pattern, llm_output, re.DOTALL)

    if not matches:
        # Fallback: Try to find any JSON-like content in the output
        json_pattern = r"\{.*?\}"
        matches = re.findall(json_pattern, llm_output, re.DOTALL)

    for json_string in matches:
        json_string = json_string.strip()
        try:
            parsed_json = json.loads(json_string)
            return parsed_json
        except json.JSONDecodeError:
            # Attempt to fix common JSON issues
            try:
                # Remove invalid control characters
                json_string_clean = re.sub(r"[\x00-\x1F\x7F]", "", json_string)
                parsed_json = json.loads(json_string_clean)
                return parsed_json
            except json.JSONDecodeError:
                continue  # Try next match

    return None  # No valid JSON found


def create_client(model):
    if model.startswith("claude-"):
        client_model = normalize_claude_model_name(model)
        if _route_claude_via_openai():
            kwargs = _resolve_claude_openai_client_kwargs()
            print(f"Using OpenAI-compatible API for Claude model {client_model}.")
            return openai.OpenAI(**kwargs), client_model
        primary_client, _ = _build_anthropic_client(
            base_url_env="ANTHROPIC_BASE_URL",
            api_key_env="ANTHROPIC_API_KEY",
            auth_token_env="ANTHROPIC_AUTH_TOKEN",
        )

        opus_client = None
        opus_model_override = os.getenv("ANTHROPIC_OPUS_MODEL")
        opus_client, opus_base_url = _build_anthropic_client(
            base_url_env="ANTHROPIC_OPUS_BASE_URL",
            api_key_env="ANTHROPIC_OPUS_API_KEY",
            auth_token_env="ANTHROPIC_OPUS_AUTH_TOKEN",
            fallback_api_key_env="ANTHROPIC_API_KEY",
            fallback_auth_token_env="ANTHROPIC_AUTH_TOKEN",
        )
        if not opus_base_url:
            opus_client = None

        backup_client = None
        backup_client, backup_base_url = _build_anthropic_client(
            base_url_env="ANTHROPIC_BACKUP_BASE_URL",
            api_key_env="ANTHROPIC_BACKUP_API_KEY",
            auth_token_env="ANTHROPIC_BACKUP_AUTH_TOKEN",
            fallback_api_key_env="ANTHROPIC_API_KEY",
            fallback_auth_token_env="ANTHROPIC_AUTH_TOKEN",
        )
        if not backup_base_url:
            backup_client = None
        backup_model_override = os.getenv("ANTHROPIC_BACKUP_MODEL")

        fallback_model = None
        if _is_truthy_env("ANTHROPIC_ENABLE_SONNET_FALLBACK", "0"):
            fallback_model = fallback_claude_model_name(client_model)

        if opus_client is not None or backup_client is not None or fallback_model is not None:
            mode_notes = []
            if opus_client is not None:
                mode_notes.append("opus-priority endpoint failover enabled")
            if backup_client is not None:
                mode_notes.append("backup endpoint failover enabled")
            if fallback_model is not None:
                mode_notes.append(f"model failover {client_model}->{fallback_model}")
            else:
                mode_notes.append("sonnet fallback disabled")
            mode_desc = ", ".join(mode_notes)
            print(
                f"Using Anthropic API with model {client_model} "
                f"({mode_desc})."
            )
            return _AnthropicFailoverClient(
                primary_client,
                opus_client,
                backup_client,
                primary_model=client_model,
                fallback_model=fallback_model,
                opus_model_override=opus_model_override,
                backup_model_override=backup_model_override,
            ), client_model

        print(f"Using Anthropic API with model {client_model}.")
        return primary_client, client_model
    elif model.startswith("bedrock") and "claude" in model:
        client_model = model.split("/")[-1]
        print(f"Using Amazon Bedrock with model {client_model}.")
        return anthropic.AnthropicBedrock(), client_model
    elif model.startswith("vertex_ai") and "claude" in model:
        client_model = model.split("/")[-1]
        print(f"Using Vertex AI with model {client_model}.")
        return anthropic.AnthropicVertex(), client_model
    elif 'gpt' in model or "o1" in model or "o3" in model:
        kwargs = _resolve_openai_client_kwargs(model)
        print(f"Using OpenAI API with model {model}.")
        return openai.OpenAI(**kwargs), model
    elif model.startswith("grok-"):
        kwargs = {}
        if os.getenv("GROK_API_KEY"):
            kwargs["api_key"] = os.environ["GROK_API_KEY"]
        elif os.getenv("OPENAI_API_KEY"):
            kwargs["api_key"] = os.environ["OPENAI_API_KEY"]
        if os.getenv("GROK_BASE_URL"):
            kwargs["base_url"] = os.environ["GROK_BASE_URL"]
        elif os.getenv("OPENAI_BASE_URL"):
            kwargs["base_url"] = os.environ["OPENAI_BASE_URL"]
        kwargs["default_headers"] = _default_openai_headers()
        print(f"Using OpenAI-compatible API for model {model}.")
        return openai.OpenAI(**kwargs), model
    elif model in ["deepseek-chat", "deepseek-reasoner", "deepseek-coder"]:
        print(f"Using OpenAI API with {model}.")
        return openai.OpenAI(
            api_key=***redacted***"DEEPSEEK_API_KEY"],
            base_url="https://api.deepseek.com"
        ), model
    elif model == "llama3.1-405b":
        print(f"Using OpenAI API with {model}.")
        return openai.OpenAI(
            api_key=***redacted***"OPENROUTER_API_KEY"],
            base_url="https://openrouter.ai/api/v1"
        ), "meta-llama/llama-3.1-405b-instruct"
    elif "gemini" in model:
        kwargs = {}
        if os.getenv("GEMINI_API_KEY"):
            kwargs["api_key"] = os.environ["GEMINI_API_KEY"]
        elif os.getenv("OPENAI_API_KEY"):
            kwargs["api_key"] = os.environ["OPENAI_API_KEY"]
        if os.getenv("GEMINI_BASE_URL"):
            kwargs["base_url"] = os.environ["GEMINI_BASE_URL"]
        else:
            kwargs["base_url"] = "https://generativelanguage.googleapis.com/v1beta/openai/"
        kwargs["default_headers"] = _default_openai_headers()
        print(f"Using OpenAI API with {model}.")
        return openai.OpenAI(**kwargs), model
    else:
        raise ValueError(f"Model {model} not supported.")
