from __future__ import annotations

import re
import threading
from collections.abc import Callable
from typing import Any

from .base import ComputeBackend
from .cloud_ssh import CloudSSHBackend
from .docker import DockerBackend
from .kubernetes import KubernetesBackend
from .local import LocalBackend
from .slurm import SlurmBackend
from .ssh import SSHBackend

BackendFactory = Callable[..., ComputeBackend]


def _normalize(name: str) -> str:
    normalized = re.sub(r"[-_\s]+", "-", name.strip().lower())
    if not normalized:
        raise ValueError("backend name cannot be empty")
    return normalized


class ComputeBackendRegistry:
    def __init__(self) -> None:
        self._factories: dict[str, BackendFactory] = {}
        self._aliases: dict[str, str] = {}
        self._lock = threading.RLock()

    def register(
        self,
        name: str,
        factory: BackendFactory,
        *,
        aliases: tuple[str, ...] = (),
    ) -> None:
        canonical = _normalize(name)
        normalized_aliases = tuple(_normalize(alias) for alias in aliases)
        with self._lock:
            collisions = [
                candidate
                for candidate in (canonical, *normalized_aliases)
                if candidate in self._factories or candidate in self._aliases
            ]
            if collisions:
                raise ValueError(f"backend already registered: {collisions[0]}")
            self._factories[canonical] = factory
            for alias in normalized_aliases:
                self._aliases[alias] = canonical

    def create(self, name: str, *args: Any, **kwargs: Any) -> ComputeBackend:
        normalized = _normalize(name)
        with self._lock:
            canonical = self._aliases.get(normalized, normalized)
            try:
                factory = self._factories[canonical]
            except KeyError as exc:
                available = ", ".join(self.names())
                raise KeyError(f"unknown compute backend {name!r}; available: {available}") from exc
        backend = factory(*args, **kwargs)
        if not isinstance(backend, ComputeBackend):
            raise TypeError(f"backend factory {canonical!r} returned {type(backend)!r}")
        return backend

    def names(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._factories))


backend_registry = ComputeBackendRegistry()
backend_registry.register("local", LocalBackend)
backend_registry.register("docker", DockerBackend, aliases=("container",))
backend_registry.register("ssh", SSHBackend)
backend_registry.register("slurm", SlurmBackend)
backend_registry.register("kubernetes", KubernetesBackend, aliases=("k8s",))
backend_registry.register("cloud-ssh", CloudSSHBackend, aliases=("cloud_ssh",))


def create_backend(name: str, *args: Any, **kwargs: Any) -> ComputeBackend:
    return backend_registry.create(name, *args, **kwargs)
