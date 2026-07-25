from __future__ import annotations

import re
import threading
from collections.abc import Iterable, Mapping
from typing import Any

from .base import DomainPlugin
from .bio import BioPlugin
from .contracts import PluginResult
from .cv import CVPlugin
from .nlp import NLPPlugin
from .physics_material import PhysicsMaterialPlugin
from .rl import RLPlugin
from .robotics import RoboticsPlugin


def normalize_plugin_name(name: str) -> str:
    if not isinstance(name, str):
        raise TypeError("plugin name must be a string")
    normalized = re.sub(r"[-_\s]+", "-", name.strip().lower())
    if not normalized:
        raise ValueError("plugin name cannot be empty")
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", normalized):
        raise ValueError(f"plugin name contains unsafe characters: {name!r}")
    return normalized


class DomainPluginRegistry:
    def __init__(self) -> None:
        self._plugins: dict[str, DomainPlugin] = {}
        self._aliases: dict[str, str] = {}
        self._lock = threading.RLock()

    def register(
        self,
        plugin: DomainPlugin,
        *,
        name: str | None = None,
        aliases: Iterable[str] = (),
    ) -> DomainPlugin:
        if not isinstance(plugin, DomainPlugin):
            raise TypeError("plugin must be a DomainPlugin")
        canonical = normalize_plugin_name(name or plugin.name)
        normalized_aliases = tuple(normalize_plugin_name(alias) for alias in aliases)
        if canonical in normalized_aliases:
            raise ValueError(f"plugin alias duplicates canonical name: {canonical}")
        with self._lock:
            for candidate in (canonical, *normalized_aliases):
                if candidate in self._plugins or candidate in self._aliases:
                    raise ValueError(f"plugin already registered: {candidate}")
            self._plugins[canonical] = plugin
            for alias in normalized_aliases:
                self._aliases[alias] = canonical
        return plugin

    def unregister(self, name: str) -> DomainPlugin:
        normalized = normalize_plugin_name(name)
        with self._lock:
            canonical = self._aliases.get(normalized, normalized)
            try:
                plugin = self._plugins.pop(canonical)
            except KeyError as exc:
                raise KeyError(f"unknown domain plugin: {name}") from exc
            self._aliases = {
                alias: target for alias, target in self._aliases.items() if target != canonical
            }
            return plugin

    def get(self, name: str) -> DomainPlugin:
        normalized = normalize_plugin_name(name)
        with self._lock:
            canonical = self._aliases.get(normalized, normalized)
            try:
                return self._plugins[canonical]
            except KeyError as exc:
                available = ", ".join(self.names())
                raise KeyError(f"unknown domain plugin {name!r}; available: {available}") from exc

    def names(self, *, include_aliases: bool = False) -> tuple[str, ...]:
        with self._lock:
            names = set(self._plugins)
            if include_aliases:
                names.update(self._aliases)
            return tuple(sorted(names))

    def items(self) -> tuple[tuple[str, DomainPlugin], ...]:
        with self._lock:
            return tuple((name, self._plugins[name]) for name in sorted(self._plugins))

    def list_plugins(self) -> tuple[str, ...]:
        return self.names()

    def run(
        self,
        name: str,
        rows: Iterable[Mapping[str, Any]],
    ) -> PluginResult:
        return self.get(name).run(rows)


def create_builtin_registry() -> DomainPluginRegistry:
    registry = DomainPluginRegistry()
    registry.register(
        CVPlugin(),
        aliases=("computer-vision", "vision"),
    )
    registry.register(
        NLPPlugin(),
        aliases=("natural-language-processing",),
    )
    registry.register(
        RLPlugin(),
        aliases=("reinforcement-learning",),
    )
    registry.register(
        BioPlugin(),
        aliases=("biology", "bioinformatics"),
    )
    registry.register(
        PhysicsMaterialPlugin(),
        aliases=("materials", "materials-science", "physics-materials"),
    )
    registry.register(
        RoboticsPlugin(),
        aliases=("robot", "robot-learning"),
    )
    return registry


builtin_registry = create_builtin_registry()
default_registry = builtin_registry
plugin_registry = builtin_registry


def get_plugin(name: str) -> DomainPlugin:
    return builtin_registry.get(name)


def run_plugin(
    name: str,
    rows: Iterable[Mapping[str, Any]],
) -> PluginResult:
    return builtin_registry.run(name, rows)
