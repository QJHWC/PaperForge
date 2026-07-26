from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable

from engine.secret_redaction import redact_structure

from .artifacts import ArtifactStore, sha256_file
from .models import utc_now

TRACE_SCHEMA_VERSION = 1
_TRACE_NAME_PATTERN = re.compile(r"[^a-zA-Z0-9_.-]+")


class AgentError(RuntimeError):
    """Base class for agent runtime contract failures."""


class AgentContractError(AgentError, ValueError):
    """Raised when an agent request or result violates the typed contract."""


class AgentNotRegisteredError(AgentError, KeyError):
    """Raised when no agent is registered for a requested role."""


class AgentTraceIntegrityError(AgentError):
    """Raised when a persisted trace input is missing or has changed."""


class AgentRole(str, Enum):
    RESEARCH = "research"
    EXPERIMENT = "experiment"
    CODE = "code"
    COMPUTE = "compute"
    ANALYSIS = "analysis"
    VISUALIZATION = "visualization"
    PAPER = "paper"
    REVIEWER = "reviewer"
    RELEASE = "release"


class AgentResultStatus(str, Enum):
    ACCEPTED = "ACCEPTED"
    COMPLETED = "COMPLETED"
    SUCCESS = "COMPLETED"
    SKIPPED = "SKIPPED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


def _request_id() -> str:
    return f"req_{uuid.uuid4().hex}"


def _normalize_role(value: AgentRole | str) -> AgentRole:
    if isinstance(value, AgentRole):
        return value
    normalized = str(value).strip().lower().replace("_agent", "")
    return AgentRole(normalized)


def _safe_relative_artifact_path(value: str) -> str:
    path = Path(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise AgentContractError(f"artifact path must be workspace-relative: {value!r}")
    return path.as_posix()


@dataclass(frozen=True)
class TraceInput:
    trace_id: str
    path: str
    sha256: str
    size_bytes: int
    created_at: str
    media_type: str = "application/json"
    schema_version: int = TRACE_SCHEMA_VERSION
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.trace_id:
            raise AgentContractError("trace_id is required")
        _safe_relative_artifact_path(self.path)
        if len(self.sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.sha256.lower()
        ):
            raise AgentContractError("trace sha256 must be a 64-character hexadecimal digest")
        if self.size_bytes < 0:
            raise AgentContractError("trace size_bytes cannot be negative")
        if self.schema_version != TRACE_SCHEMA_VERSION:
            raise AgentContractError(
                f"unsupported trace schema version: {self.schema_version}"
            )
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["metadata"] = dict(self.metadata)
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> TraceInput:
        return cls(
            trace_id=str(payload["trace_id"]),
            path=str(payload["path"]),
            sha256=str(payload["sha256"]),
            size_bytes=int(payload["size_bytes"]),
            created_at=str(payload["created_at"]),
            media_type=str(payload.get("media_type") or "application/json"),
            schema_version=int(payload.get("schema_version", TRACE_SCHEMA_VERSION)),
            metadata=dict(payload.get("metadata") or {}),
        )


# A descriptive alias for callers that want to make durability explicit.
PersistentTraceInput = TraceInput


class PersistentTraceStore:
    """Persists JSON trace envelopes and returns hash-bound input references."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        trace_root: str = ".paperforge/traces",
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.trace_root = trace_root.rstrip("/")
        self._artifacts = ArtifactStore(
            self.workspace,
            allowed_roots=(self.trace_root,),
            allowed_suffixes=(".json",),
            allowed_kinds=("trace",),
        )

    @staticmethod
    def _normalize_name(name: str) -> str:
        normalized = _TRACE_NAME_PATTERN.sub("-", str(name).strip()).strip(".-_")
        if not normalized:
            raise AgentContractError("trace name must contain an alphanumeric character")
        return normalized[:96]

    def persist(
        self,
        payload: Mapping[str, Any] | Sequence[Any],
        *,
        name: str = "trace-input",
        metadata: Mapping[str, Any] | None = None,
    ) -> TraceInput:
        normalized_name = self._normalize_name(name)
        created_at = utc_now()
        trace_id = f"trace_{uuid.uuid4().hex}"
        safe_payload = redact_structure(payload)
        safe_metadata = redact_structure(dict(metadata or {}))
        envelope: dict[str, Any] = {
            "schema_version": TRACE_SCHEMA_VERSION,
            "trace_id": trace_id,
            "created_at": created_at,
            "payload": safe_payload,
            "metadata": safe_metadata,
        }
        canonical = json.dumps(
            envelope,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        content_hint = hashlib.sha256(canonical).hexdigest()[:12]
        relative_path = f"{self.trace_root}/{normalized_name}-{content_hint}.json"
        record = self._artifacts.write_json(
            relative_path,
            envelope,
            kind="trace",
            metadata=safe_metadata,
        )
        return TraceInput(
            trace_id=trace_id,
            path=record.path,
            sha256=record.sha256,
            size_bytes=record.size_bytes,
            created_at=created_at,
            metadata=safe_metadata,
        )

    def verify(self, trace: TraceInput) -> bool:
        try:
            path = self._artifacts.resolve(trace.path, kind="trace")
        except (OSError, ValueError) as exc:
            raise AgentTraceIntegrityError(f"invalid persisted trace path: {trace.path}") from exc
        if path.is_symlink() or not path.is_file():
            raise AgentTraceIntegrityError(f"persisted trace is missing: {trace.path}")
        if path.stat().st_size != trace.size_bytes or sha256_file(path) != trace.sha256:
            raise AgentTraceIntegrityError(f"persisted trace digest mismatch: {trace.path}")
        return True

    def load(self, trace: TraceInput) -> Any:
        self.verify(trace)
        path = self._artifacts.resolve(trace.path, kind="trace")
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AgentTraceIntegrityError(f"persisted trace is invalid JSON: {trace.path}") from exc
        if (
            not isinstance(envelope, dict)
            or envelope.get("schema_version") != TRACE_SCHEMA_VERSION
            or envelope.get("trace_id") != trace.trace_id
            or "payload" not in envelope
        ):
            raise AgentTraceIntegrityError(f"persisted trace envelope mismatch: {trace.path}")
        return envelope["payload"]


@dataclass(frozen=True)
class AgentRequest:
    role: AgentRole | str
    task: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    trace_inputs: tuple[TraceInput, ...] = ()
    request_id: str = field(default_factory=_request_id)

    def __post_init__(self) -> None:
        try:
            role = _normalize_role(self.role)
        except ValueError as exc:
            raise AgentContractError(f"unknown agent role: {self.role!r}") from exc
        if not str(self.task).strip():
            raise AgentContractError("agent task cannot be empty")
        if not isinstance(self.payload, Mapping):
            raise AgentContractError("agent payload must be a mapping")
        if not self.request_id:
            raise AgentContractError("request_id cannot be empty")
        traces = tuple(self.trace_inputs)
        if any(not isinstance(trace, TraceInput) for trace in traces):
            raise AgentContractError("trace_inputs must contain TraceInput references")
        object.__setattr__(self, "role", role)
        object.__setattr__(self, "task", str(self.task).strip())
        object.__setattr__(self, "payload", dict(self.payload))
        object.__setattr__(self, "trace_inputs", traces)

    @property
    def agent(self) -> AgentRole:
        return cast(AgentRole, self.role)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "role": cast(AgentRole, self.role).value,
            "task": self.task,
            "payload": dict(self.payload),
            "trace_inputs": [trace.to_dict() for trace in self.trace_inputs],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> AgentRequest:
        return cls(
            request_id=str(payload.get("request_id") or _request_id()),
            role=str(payload["role"]),
            task=str(payload["task"]),
            payload=dict(payload.get("payload") or {}),
            trace_inputs=tuple(
                TraceInput.from_dict(item) for item in payload.get("trace_inputs") or ()
            ),
        )


@dataclass(frozen=True)
class AgentResult:
    request_id: str
    role: AgentRole | str
    status: AgentResultStatus | str
    output: Mapping[str, Any] = field(default_factory=dict)
    artifacts: tuple[str, ...] = ()
    trace_outputs: tuple[TraceInput, ...] = ()
    error: str | None = None

    def __post_init__(self) -> None:
        try:
            role = _normalize_role(self.role)
            status = (
                self.status
                if isinstance(self.status, AgentResultStatus)
                else AgentResultStatus(str(self.status).upper())
            )
        except ValueError as exc:
            raise AgentContractError("invalid agent result role or status") from exc
        if not self.request_id:
            raise AgentContractError("agent result request_id cannot be empty")
        if not isinstance(self.output, Mapping):
            raise AgentContractError("agent result output must be a mapping")
        artifacts = tuple(_safe_relative_artifact_path(path) for path in self.artifacts)
        trace_outputs = tuple(self.trace_outputs)
        if any(not isinstance(trace, TraceInput) for trace in trace_outputs):
            raise AgentContractError("trace_outputs must contain TraceInput references")
        if status is AgentResultStatus.FAILED and not self.error:
            raise AgentContractError("failed agent results require an error")
        if status is not AgentResultStatus.FAILED and self.error:
            raise AgentContractError("only failed agent results may contain an error")
        object.__setattr__(self, "role", role)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "output", dict(self.output))
        object.__setattr__(self, "artifacts", artifacts)
        object.__setattr__(self, "trace_outputs", trace_outputs)

    @property
    def payload(self) -> Mapping[str, Any]:
        return self.output

    @classmethod
    def accepted(
        cls,
        request: AgentRequest,
        output: Mapping[str, Any] | None = None,
    ) -> AgentResult:
        return cls(
            request_id=request.request_id,
            role=request.role,
            status=AgentResultStatus.ACCEPTED,
            output=dict(output or {}),
        )

    @classmethod
    def completed(
        cls,
        request: AgentRequest,
        output: Mapping[str, Any] | None = None,
        *,
        artifacts: Iterable[str] = (),
        trace_outputs: Iterable[TraceInput] = (),
    ) -> AgentResult:
        return cls(
            request_id=request.request_id,
            role=request.role,
            status=AgentResultStatus.COMPLETED,
            output=dict(output or {}),
            artifacts=tuple(artifacts),
            trace_outputs=tuple(trace_outputs),
        )

    @classmethod
    def skipped(
        cls,
        request: AgentRequest,
        output: Mapping[str, Any] | None = None,
    ) -> AgentResult:
        return cls(
            request_id=request.request_id,
            role=request.role,
            status=AgentResultStatus.SKIPPED,
            output=dict(output or {}),
        )

    @classmethod
    def failed(cls, request: AgentRequest, error: str) -> AgentResult:
        return cls(
            request_id=request.request_id,
            role=request.role,
            status=AgentResultStatus.FAILED,
            error=str(error),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "role": cast(AgentRole, self.role).value,
            "status": cast(AgentResultStatus, self.status).value,
            "output": dict(self.output),
            "artifacts": list(self.artifacts),
            "trace_outputs": [trace.to_dict() for trace in self.trace_outputs],
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> AgentResult:
        return cls(
            request_id=str(payload["request_id"]),
            role=str(payload["role"]),
            status=str(payload["status"]),
            output=dict(payload.get("output") or {}),
            artifacts=tuple(str(path) for path in payload.get("artifacts") or ()),
            trace_outputs=tuple(
                TraceInput.from_dict(item) for item in payload.get("trace_outputs") or ()
            ),
            error=str(payload["error"]) if payload.get("error") is not None else None,
        )


AgentHandler = Callable[
    [AgentRequest, tuple[Any, ...]],
    AgentResult | Mapping[str, Any],
]


@runtime_checkable
class Agent(Protocol):
    role: AgentRole

    def handle(
        self,
        request: AgentRequest,
        trace_payloads: tuple[Any, ...] = (),
    ) -> AgentResult:
        ...


class RoutedAgent:
    role: AgentRole

    def __init__(self, handler: AgentHandler | None = None) -> None:
        self._handler = handler

    def handle(
        self,
        request: AgentRequest,
        trace_payloads: tuple[Any, ...] = (),
    ) -> AgentResult:
        if request.role is not self.role:
            raise AgentContractError(
                f"{self.role.value} agent cannot handle "
                f"{cast(AgentRole, request.role).value} request"
            )
        if self._handler is None:
            return AgentResult(
                request_id=request.request_id,
                role=request.role,
                status=AgentResultStatus.BLOCKED,
                output={
                    "reason": "handler_not_configured",
                    "role": self.role.value,
                    "trace_input_count": len(trace_payloads),
                },
            )
        result = self._handler(request, trace_payloads)
        if isinstance(result, AgentResult):
            return result
        if isinstance(result, Mapping):
            return AgentResult.completed(request, result)
        raise AgentContractError(
            f"{self.role.value} agent handler returned an unsupported result"
        )

    run = handle


class ResearchAgent(RoutedAgent):
    role = AgentRole.RESEARCH


class ExperimentAgent(RoutedAgent):
    role = AgentRole.EXPERIMENT


class CodeAgent(RoutedAgent):
    role = AgentRole.CODE


class ComputeAgent(RoutedAgent):
    role = AgentRole.COMPUTE


class AnalysisAgent(RoutedAgent):
    role = AgentRole.ANALYSIS


class VisualizationAgent(RoutedAgent):
    role = AgentRole.VISUALIZATION


class PaperAgent(RoutedAgent):
    role = AgentRole.PAPER


class ReviewerAgent(RoutedAgent):
    role = AgentRole.REVIEWER


class ReleaseAgent(RoutedAgent):
    role = AgentRole.RELEASE


_DEFAULT_AGENT_TYPES = (
    ResearchAgent,
    ExperimentAgent,
    CodeAgent,
    ComputeAgent,
    AnalysisAgent,
    VisualizationAgent,
    PaperAgent,
    ReviewerAgent,
    ReleaseAgent,
)


class AgentRegistry:
    def __init__(
        self,
        agents: Iterable[Agent] | None = None,
        *,
        trace_store: PersistentTraceStore | None = None,
    ) -> None:
        self.trace_store = trace_store
        self._agents: dict[AgentRole, Agent] = {}
        configured_agents = (
            (agent_type() for agent_type in _DEFAULT_AGENT_TYPES)
            if agents is None
            else agents
        )
        for agent in configured_agents:
            self.register(agent)

    @classmethod
    def with_defaults(
        cls,
        *,
        trace_store: PersistentTraceStore | None = None,
    ) -> AgentRegistry:
        return cls(trace_store=trace_store)

    @classmethod
    def default(
        cls,
        *,
        trace_store: PersistentTraceStore | None = None,
    ) -> AgentRegistry:
        return cls.with_defaults(trace_store=trace_store)

    def register(self, agent: Agent, *, replace: bool = False) -> None:
        if not isinstance(agent, Agent):
            raise AgentContractError("registered object does not implement the Agent protocol")
        role = _normalize_role(agent.role)
        if role in self._agents and not replace:
            raise AgentContractError(f"agent role is already registered: {role.value}")
        self._agents[role] = agent

    def resolve(self, role: AgentRole | str) -> Agent:
        normalized = _normalize_role(role)
        try:
            return self._agents[normalized]
        except KeyError as exc:
            raise AgentNotRegisteredError(normalized.value) from exc

    get = resolve

    @property
    def roles(self) -> frozenset[AgentRole]:
        return frozenset(self._agents)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(role.value for role in self._agents))

    def dispatch(self, request: AgentRequest) -> AgentResult:
        if not isinstance(request, AgentRequest):
            raise AgentContractError("dispatch requires an AgentRequest")
        if request.trace_inputs and self.trace_store is None:
            raise AgentTraceIntegrityError(
                "trace inputs require a configured PersistentTraceStore"
            )
        trace_payloads = (
            tuple(self.trace_store.load(trace) for trace in request.trace_inputs)
            if self.trace_store is not None
            else ()
        )
        result = self.resolve(request.role).handle(request, trace_payloads)
        if not isinstance(result, AgentResult):
            raise AgentContractError("agent returned a non-AgentResult value")
        if result.request_id != request.request_id:
            raise AgentContractError("agent result request_id does not match request")
        if result.role is not request.role:
            raise AgentContractError("agent result role does not match request")
        return result

    route = dispatch

    def __contains__(self, role: object) -> bool:
        try:
            return _normalize_role(role) in self._agents  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False

    def __len__(self) -> int:
        return len(self._agents)


__all__ = [
    "Agent",
    "AgentContractError",
    "AgentError",
    "AgentHandler",
    "AgentNotRegisteredError",
    "AgentRegistry",
    "AgentRequest",
    "AgentResult",
    "AgentResultStatus",
    "AgentRole",
    "AgentTraceIntegrityError",
    "AnalysisAgent",
    "CodeAgent",
    "ComputeAgent",
    "ExperimentAgent",
    "PaperAgent",
    "PersistentTraceInput",
    "PersistentTraceStore",
    "ReleaseAgent",
    "ResearchAgent",
    "ReviewerAgent",
    "RoutedAgent",
    "TRACE_SCHEMA_VERSION",
    "TraceInput",
    "VisualizationAgent",
]
