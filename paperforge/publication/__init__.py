"""Fail-closed, offline-capable publication engine for PaperForge v3."""

from .bibliography import (
    REFERENCES_BIB,
    BibliographyContract,
    BibliographyContractError,
    validate_single_references_bib,
)
from .bundle import (
    DEFAULT_SOURCE_SUFFIXES,
    SOURCE_BUNDLE_SCHEMA,
    SOURCE_LOCK_SCHEMA,
    BundleResult,
    LockMismatch,
    SourceBundler,
    SourceLockVerification,
    verify_bundle_checksum,
    verify_source_lock,
)
from .compiler import PublicationCompiler
from .diagnostics import ConstrainedLayoutRepairer, DefaultLayoutDiagnostician
from .engine import (
    PUBLICATION_MANIFEST_SCHEMA,
    PublicationEngine,
    PublicationGateError,
    publish,
)
from .invariants import InvariantSnapshot, PublicationInvariantViolation
from .models import (
    CommandResult,
    CompileResult,
    LayoutDiagnosis,
    PublicationIssue,
    PublicationRound,
    PublicationRunResult,
    RenderResult,
    RepairContext,
    RepairProposal,
)
from .profiles import (
    DEFAULT_TEMPLATE_REGISTRY,
    TemplateProfile,
    TemplateProfileRegistry,
    UnknownTemplateProfile,
)
from .renderer import PopplerRenderer
from .toolchain import (
    Toolchain,
    ToolchainDiscoveryError,
    discover_toolchain,
)

__all__ = [
    "DEFAULT_SOURCE_SUFFIXES",
    "DEFAULT_TEMPLATE_REGISTRY",
    "PUBLICATION_MANIFEST_SCHEMA",
    "REFERENCES_BIB",
    "SOURCE_BUNDLE_SCHEMA",
    "SOURCE_LOCK_SCHEMA",
    "BibliographyContract",
    "BibliographyContractError",
    "BundleResult",
    "CommandResult",
    "CompileResult",
    "ConstrainedLayoutRepairer",
    "DefaultLayoutDiagnostician",
    "InvariantSnapshot",
    "LayoutDiagnosis",
    "LockMismatch",
    "PopplerRenderer",
    "PublicationCompiler",
    "PublicationEngine",
    "PublicationGateError",
    "PublicationInvariantViolation",
    "PublicationIssue",
    "PublicationRound",
    "PublicationRunResult",
    "RenderResult",
    "RepairContext",
    "RepairProposal",
    "SourceBundler",
    "SourceLockVerification",
    "TemplateProfile",
    "TemplateProfileRegistry",
    "Toolchain",
    "ToolchainDiscoveryError",
    "UnknownTemplateProfile",
    "discover_toolchain",
    "publish",
    "validate_single_references_bib",
    "verify_bundle_checksum",
    "verify_source_lock",
]
