from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

REDACTION_MARKER = "***redacted***"

_SECRET_ENV_KEY_PATTERN = re.compile(
    r"(?i)(?:^|_)(?:"
    r"api_?key|auth_?token|access_?token|refresh_?token|token|"
    r"password|passphrase|secret|private_?key|access_?key|credential"
    r")(?:$|_)"
)
_NON_SECRET_METADATA_FIELDS = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "total_tokens",
        "max_tokens",
        "token_budget",
        "token_count",
    }
)
_SECRET_FIELD_PATTERN = re.compile(
    r"(?i)(?:^|_)(?:"
    r"api_?key|auth_?token|access_?token|refresh_?token|token|"
    r"password|passphrase|client_?secret|secret|private_?key|access_?key|"
    r"authorization|cookie|credentials?"
    r")(?:_(?:data|file|path|raw|text|value))?$"
)
_PRIVATE_KEY_PATTERN = re.compile(
    r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
    re.DOTALL,
)
_FULL_SECRET_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9._-]{8,}"),
    re.compile(r"(?<![A-Za-z0-9])sk_(?:live|test)_[A-Za-z0-9]{8,}"),
    re.compile(r"(?<![A-Za-z0-9])AKIA[A-Z0-9]{16}"),
    re.compile(r"(?<![A-Za-z0-9])hf_[A-Za-z0-9]{16,}"),
    re.compile(r"(?<![A-Za-z0-9])github_pat_[A-Za-z0-9_]{16,}"),
    re.compile(r"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"(?<![A-Za-z0-9])glpat-[A-Za-z0-9_-]{12,}"),
    re.compile(r"(?<![A-Za-z0-9])xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"(?<![A-Za-z0-9])AIza[A-Za-z0-9_-]{20,}"),
    re.compile(
        r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}"
        r"\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    ),
)
_PREFIX_SECRET_PATTERNS = (
    re.compile(r"(?i)(\bbearer\s+[\"']?)[^\s,;\"']+"),
    re.compile(
        r"(?i)(authorization\s*[:=]\s*(?:bearer|basic)\s+[\"']?)"
        r"[^\s,;\"']+"
    ),
    re.compile(r"(?i)((?:set-)?cookie\s*:\s*)[^\r\n]+"),
    re.compile(
        r"(?i)((?:"
        r"api[_-]?key|auth(?:entication)?[_-]?token|access[_-]?token|token|"
        r"refresh[_-]?token|password|passphrase|client[_-]?secret|"
        r"secret|private[_-]?key|secret[_-]?access[_-]?key|credentials?"
        r")\s*[:=]\s*[\"']?)[^\s,;\"'&}]+"
    ),
    re.compile(
        r"(?i)((?:^|\s)--[a-z0-9_-]*(?:"
        r"api[-_]?key|auth[-_]?token|access[-_]?token|refresh[-_]?token|token|"
        r"password|passphrase|secret|private[-_]?key|credentials?"
        r")(?:=|\s+)[\"']?)[^\s,;\"']+"
    ),
    re.compile(r"(?i)([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@"),
)


def _normalize_field_name(name: object) -> str:
    rendered = re.sub(
        r"(?<=[a-z0-9])(?=[A-Z])",
        "_",
        str(name).strip(),
    )
    return re.sub(r"[^a-z0-9]+", "_", rendered.lower()).strip("_")


def _is_secret_field(name: object) -> bool:
    normalized = _normalize_field_name(name)
    if normalized in _NON_SECRET_METADATA_FIELDS:
        return False
    return bool(_SECRET_FIELD_PATTERN.search(normalized))


def secret_values_from_env(
    env: Mapping[str, object] | None,
) -> tuple[str, ...]:
    """Extract only values whose environment variable names are secret-like."""

    if not env:
        return ()
    return tuple(
        str(value)
        for key, value in env.items()
        if _SECRET_ENV_KEY_PATTERN.search(str(key))
        and value is not None
        and len(str(value)) >= 4
    )


def secret_values_from_structure(value: Any) -> tuple[str, ...]:
    """Collect values held in explicitly secret-labelled metadata fields."""

    def leaf_values(item: Any) -> list[str]:
        if isinstance(item, Mapping):
            leaves: list[str] = []
            for nested in item.values():
                leaves.extend(leaf_values(nested))
            return leaves
        if isinstance(item, list | tuple | set | frozenset):
            leaves = []
            for nested in item:
                leaves.extend(leaf_values(nested))
            return leaves
        rendered = (
            os.fspath(item)
            if isinstance(item, os.PathLike)
            else str(item)
            if item is not None
            else ""
        )
        return [rendered] if rendered and len(rendered) >= 4 else []

    collected: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _is_secret_field(key):
                collected.extend(leaf_values(item))
            else:
                collected.extend(secret_values_from_structure(item))
    elif isinstance(value, list | tuple | set | frozenset):
        for item in value:
            collected.extend(secret_values_from_structure(item))
    return tuple(collected)


def _configured_secret_values(secret_values: Iterable[str]) -> list[str]:
    configured: list[str] = []
    configured.extend(secret_values_from_env(os.environ))
    for value in secret_values:
        rendered = str(value) if value is not None else ""
        if rendered and len(rendered) >= 4:
            configured.append(rendered)
    return sorted(set(configured), key=len, reverse=True)


def redact_secrets(text: object, *, secret_values: Iterable[str] = ()) -> str:
    """Redact configured credentials and common token forms."""

    sanitized = str(text)
    for value in _configured_secret_values(secret_values):
        sanitized = sanitized.replace(value, REDACTION_MARKER)
    sanitized = _PRIVATE_KEY_PATTERN.sub(REDACTION_MARKER, sanitized)
    for pattern in _FULL_SECRET_PATTERNS:
        sanitized = pattern.sub(REDACTION_MARKER, sanitized)
    for pattern in _PREFIX_SECRET_PATTERNS:
        sanitized = pattern.sub(
            lambda match: f"{match.group(1)}{REDACTION_MARKER}",
            sanitized,
        )
    return sanitized


def redact_command(
    command: Sequence[object] | str,
    *,
    secret_values: Iterable[str] = (),
) -> list[str]:
    """Return an argv-shaped copy with secret flags and values removed."""

    values = tuple(secret_values)
    parts: list[object] = [command] if isinstance(command, str) else list(command)
    sanitized: list[str] = []
    redact_next = False

    for raw_part in parts:
        part = str(raw_part)
        if redact_next:
            sanitized.append(REDACTION_MARKER)
            redact_next = False
            continue

        if part.startswith("-"):
            flag, separator, _value = part.partition("=")
            if _is_secret_field(flag.lstrip("-")):
                if separator:
                    sanitized.append(f"{flag}={REDACTION_MARKER}")
                else:
                    sanitized.append(flag)
                    redact_next = True
                continue

        sanitized.append(redact_secrets(part, secret_values=values))

    return sanitized


def redact_structure(
    value: Any,
    *,
    secret_values: Iterable[str] = (),
) -> Any:
    """Recursively redact JSON-like diagnostic metadata without mutating it."""

    values = tuple(
        {
            *secret_values,
            *secret_values_from_structure(value),
        }
    )
    if isinstance(value, Mapping):
        sanitized: dict[Any, Any] = {}
        for key, item in value.items():
            normalized_key = _normalize_field_name(key)
            sanitized_key = (
                redact_secrets(
                    os.fspath(key),
                    secret_values=values,
                )
                if isinstance(key, str | os.PathLike)
                else key
            )
            if _is_secret_field(key) and item not in (None, ""):
                sanitized[sanitized_key] = REDACTION_MARKER
            elif normalized_key in {"command", "argv", "args"} and isinstance(
                item, str | Sequence
            ):
                if isinstance(item, str):
                    sanitized[sanitized_key] = redact_secrets(
                        item,
                        secret_values=values,
                    )
                else:
                    safe_command = redact_command(
                        item,
                        secret_values=values,
                    )
                    sanitized[sanitized_key] = (
                        tuple(safe_command)
                        if isinstance(item, tuple)
                        else safe_command
                    )
            else:
                sanitized[sanitized_key] = redact_structure(
                    item,
                    secret_values=values,
                )
        return sanitized
    if isinstance(value, list):
        return [redact_structure(item, secret_values=values) for item in value]
    if isinstance(value, tuple):
        return tuple(
            redact_structure(item, secret_values=values) for item in value
        )
    if isinstance(value, set):
        return {redact_structure(item, secret_values=values) for item in value}
    if isinstance(value, frozenset):
        return frozenset(
            redact_structure(item, secret_values=values) for item in value
        )
    if isinstance(value, bytes):
        return redact_secrets(
            value.decode("utf-8", errors="replace"),
            secret_values=values,
        )
    if isinstance(value, str):
        return redact_secrets(value, secret_values=values)
    if isinstance(value, os.PathLike):
        return redact_secrets(os.fspath(value), secret_values=values)
    return value


def contains_secret(value: Any) -> bool:
    """Return True when a JSON-like value would require secret redaction."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            if _is_secret_field(key) and item not in (None, ""):
                return True
            normalized_key = _normalize_field_name(key)
            if normalized_key in {"command", "argv", "args"} and isinstance(
                item, str | Sequence
            ):
                original = [item] if isinstance(item, str) else [str(part) for part in item]
                if redact_command(item) != original:
                    return True
            if contains_secret(item):
                return True
        return False
    if isinstance(value, list | tuple | set | frozenset):
        return any(contains_secret(item) for item in value)
    if isinstance(value, bytes):
        rendered = value.decode("utf-8", errors="replace")
        return redact_secrets(rendered) != rendered
    if isinstance(value, str | os.PathLike):
        rendered = os.fspath(value)
        return redact_secrets(rendered) != rendered
    return False
