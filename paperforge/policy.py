from __future__ import annotations

import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .models import ExecutionProfile


class Action(str, Enum):
    EVIDENCE_READ = "evidence.read"
    LITERATURE_SEARCH = "literature.search"
    DRAFT_EDIT = "draft.edit"
    CITATION_EDIT = "citation.edit"
    LATEX_COMPILE = "latex.compile"
    PUBLICATION_VISUAL = "publication.visual"
    PROPOSAL_CREATE = "proposal.create"
    CODE_PATCH = "code.patch"
    EXPERIMENT_STATIC = "experiment.static"
    EXPERIMENT_MINI = "experiment.mini"
    EXPERIMENT_FULL = "experiment.full"
    LOCAL_EXECUTE = "compute.local"
    CONTAINER_EXECUTE = "compute.container"
    REMOTE_EXECUTE = "compute.remote"
    DATASET_READ = "dataset.read"
    WEIGHT_READ = "weights.read"
    GITHUB_WRITE = "github.write"


class PolicyViolation(PermissionError):
    pass


_WRITING_ACTIONS = {
    Action.EVIDENCE_READ,
    Action.LITERATURE_SEARCH,
    Action.DRAFT_EDIT,
    Action.CITATION_EDIT,
    Action.LATEX_COMPILE,
    Action.PUBLICATION_VISUAL,
}
_RESEARCH_ACTIONS = _WRITING_ACTIONS | {Action.PROPOSAL_CREATE}
_FULL_ACTIONS = set(Action)

_EXPERIMENT_COMMAND_PATTERN = re.compile(
    r"(^|[/\\])(train|training|experiment|inference|evaluate|plot)([._-]|$)",
    re.IGNORECASE,
)
_WEIGHT_SUFFIXES = {".ckpt", ".onnx", ".pt", ".pth", ".safetensors"}
_DATA_SUFFIXES = {".csv", ".h5", ".hdf5", ".mat", ".npy", ".npz", ".parquet"}


@dataclass(frozen=True)
class ExecutionPolicy:
    profile: ExecutionProfile

    @classmethod
    def from_value(cls, value: str | ExecutionProfile | None) -> ExecutionPolicy:
        raw = value or os.getenv(
            "PAPERFORGE_EXECUTION_PROFILE",
            ExecutionProfile.WRITING_ONLY.value,
        )
        return cls(ExecutionProfile(raw))

    @property
    def allowed_actions(self) -> set[Action]:
        if self.profile is ExecutionProfile.WRITING_ONLY:
            return set(_WRITING_ACTIONS)
        if self.profile is ExecutionProfile.RESEARCH:
            return set(_RESEARCH_ACTIONS)
        return set(_FULL_ACTIONS)

    def require(self, action: Action, *, detail: str = "") -> None:
        if action not in self.allowed_actions:
            suffix = f": {detail}" if detail else ""
            raise PolicyViolation(
                f"{self.profile.value} profile denies {action.value}{suffix}"
            )

    def validate_command(self, command: Sequence[str], action: Action) -> None:
        self.require(action, detail=" ".join(str(part) for part in command[:3]))
        rendered = " ".join(str(part) for part in command)
        if self.profile is ExecutionProfile.WRITING_ONLY and _EXPERIMENT_COMMAND_PATTERN.search(
            rendered
        ):
            raise PolicyViolation(
                f"writing-only profile denies experimental command: {Path(command[0]).name}"
            )

    def validate_context_paths(self, paths: Iterable[str | Path]) -> None:
        if self.profile is not ExecutionProfile.WRITING_ONLY:
            return
        for raw_path in paths:
            path = Path(raw_path)
            lowered_parts = {part.lower() for part in path.parts}
            if any(part.startswith("run_") for part in lowered_parts):
                raise PolicyViolation(f"writing-only context denies run artifact: {path.name}")
            if path.suffix.lower() in _WEIGHT_SUFFIXES:
                raise PolicyViolation(f"writing-only context denies model weight: {path.name}")
            if path.suffix.lower() in _DATA_SUFFIXES:
                raise PolicyViolation(f"writing-only context denies dataset artifact: {path.name}")
            if path.name.lower() in {"experiment.py", "plot.py", "train.py", "inference.py"}:
                raise PolicyViolation(f"writing-only context denies executable artifact: {path.name}")
