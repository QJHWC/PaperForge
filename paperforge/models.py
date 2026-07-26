from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ExecutionProfile(str, Enum):
    WRITING_ONLY = "writing-only"
    RESEARCH = "research"
    FULL = "full"


class WorkflowStatus(str, Enum):
    READY = "READY"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    INTERRUPTED = "INTERRUPTED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    AUTH_BLOCKED = "AUTH_BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"


class ArtifactTier(str, Enum):
    IMPORTED_SEED = "IMPORTED_SEED"
    ONLINE_REFINED = "ONLINE_REFINED"
    FINAL_PUBLICATION = "FINAL_PUBLICATION"


class ClaimType(str, Enum):
    NON_CLAIM = "NON_CLAIM"
    STATIC_IMPLEMENTATION = "STATIC_IMPLEMENTATION"
    RUNTIME_OBSERVED = "RUNTIME_OBSERVED"
    EXPERIMENT_RESULT = "EXPERIMENT_RESULT"
    LITERATURE = "LITERATURE"
    PROVENANCE_LICENSE = "PROVENANCE_LICENSE"
    LIMITATION = "LIMITATION"


class ClaimStatus(str, Enum):
    NON_CLAIM = "NON_CLAIM"
    SUPPORTED_STATIC = "SUPPORTED_STATIC"
    VERIFIED_RUNTIME = "VERIFIED_RUNTIME"
    VERIFIED_EXPERIMENT = "VERIFIED_EXPERIMENT"
    AUTHOR_ASSERTED = "AUTHOR_ASSERTED"
    NEEDS_PRIMARY_SOURCE = "NEEDS_PRIMARY_SOURCE"
    BLOCKED = "BLOCKED"
    CONTRADICTED = "CONTRADICTED"


class ClaimRelation(str, Enum):
    SUPPORTS = "supports"
    QUALIFIES = "qualifies"
    CONTRADICTS = "contradicts"


class ExperimentStage(str, Enum):
    PROPOSAL = "PROPOSAL"
    STATIC_CHECK = "STATIC_CHECK"
    MINI_EXPERIMENT = "MINI_EXPERIMENT"
    FULL_EXPERIMENT = "FULL_EXPERIMENT"


class ExperimentStatus(str, Enum):
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    RUNNING = "RUNNING"
    PASSED = "PASSED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class CompletionGate:
    claim_gate_passed: bool = False
    required_artifacts_present: bool = False
    latex_clean_compile: bool = False
    all_pdf_pages_inspected: bool = False
    protected_hashes_unchanged: bool = False
    secret_scan_clean: bool = False
    release_manifest_verified: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        boolean_fields = (
            "claim_gate_passed",
            "required_artifacts_present",
            "latex_clean_compile",
            "all_pdf_pages_inspected",
            "protected_hashes_unchanged",
            "secret_scan_clean",
            "release_manifest_verified",
        )
        for field_name in boolean_fields:
            if not isinstance(getattr(self, field_name), bool):
                raise TypeError(f"{field_name} must be a boolean")
        if not isinstance(self.details, dict):
            raise TypeError("details must be a dictionary")

    @property
    def passed(self) -> bool:
        return all(
            (
                self.claim_gate_passed,
                self.required_artifacts_present,
                self.latex_clean_compile,
                self.all_pdf_pages_inspected,
                self.protected_hashes_unchanged,
                self.secret_scan_clean,
                self.release_manifest_verified,
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
