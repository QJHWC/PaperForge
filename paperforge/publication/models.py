from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class PublicationIssue:
    code: str
    message: str
    severity: str = "error"
    source: str | None = None
    line: int | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity.lower() == "error"

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
        }
        if self.source is not None:
            payload["source"] = self.source
        if self.line is not None:
            payload["line"] = self.line
        if self.details:
            payload["details"] = dict(self.details)
        return payload


@dataclass(frozen=True, slots=True)
class CommandResult:
    command: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "command": list(self.command),
            "returncode": self.returncode,
            "timed_out": self.timed_out,
        }


@dataclass(frozen=True, slots=True)
class CompileResult:
    success: bool
    pdf_path: Path | None
    log_path: Path
    diagnostics: tuple[PublicationIssue, ...] = ()
    commands: tuple[CommandResult, ...] = ()
    backend: str | None = None


@dataclass(frozen=True, slots=True)
class RenderResult:
    success: bool
    pages: tuple[Path, ...]
    page_count: int
    log_path: Path
    diagnostics: tuple[PublicationIssue, ...] = ()
    commands: tuple[CommandResult, ...] = ()


@dataclass(frozen=True, slots=True)
class LayoutDiagnosis:
    issues: tuple[PublicationIssue, ...] = ()

    @property
    def clean(self) -> bool:
        return not any(issue.blocking for issue in self.issues)

    def as_dict(self) -> dict[str, Any]:
        return {
            "clean": self.clean,
            "issues": [issue.as_dict() for issue in self.issues],
        }


@dataclass(frozen=True, slots=True)
class RepairProposal:
    source_text: str
    description: str = "constrained layout repair"


@dataclass(frozen=True, slots=True)
class RepairContext:
    round_number: int
    source_text: str
    tex_path: Path
    profile: Any
    diagnosis: LayoutDiagnosis
    rendered_pages: tuple[Path, ...] = ()


@dataclass(frozen=True, slots=True)
class PublicationRound:
    number: int
    compile_result: CompileResult
    render_result: RenderResult | None
    diagnosis: LayoutDiagnosis
    repair_description: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "compile_success": self.compile_result.success,
            "compile": {
                "success": self.compile_result.success,
                "backend": self.compile_result.backend,
                "log_path": str(self.compile_result.log_path),
                "commands": [
                    command.as_dict() for command in self.compile_result.commands
                ],
            },
            "render_success": (
                self.render_result.success if self.render_result is not None else False
            ),
            "render": (
                {
                    "success": self.render_result.success,
                    "page_count": self.render_result.page_count,
                    "rendered_pages": [
                        str(path) for path in self.render_result.pages
                    ],
                    "log_path": str(self.render_result.log_path),
                    "commands": [
                        command.as_dict() for command in self.render_result.commands
                    ],
                }
                if self.render_result is not None
                else None
            ),
            "diagnosis": self.diagnosis.as_dict(),
            "repair_description": self.repair_description,
        }


@dataclass(frozen=True, slots=True)
class PublicationRunResult:
    success: bool
    profile: str
    final_pdf: Path | None
    rounds: tuple[PublicationRound, ...]
    diagnostics: tuple[PublicationIssue, ...]
    manifest_path: Path | None = None
    bundle_path: Path | None = None
    source_lock_path: Path | None = None
    checksum_path: Path | None = None
    bundle_sha256: str | None = None
    gates: Mapping[str, Any] = field(default_factory=dict)

    @property
    def pdf_path(self) -> Path | None:
        return self.final_pdf

    @property
    def gates_passed(self) -> bool:
        return self.success and not any(issue.blocking for issue in self.diagnostics)

    @property
    def artifacts(self) -> dict[str, Path | str | None]:
        return {
            "pdf": self.final_pdf,
            "manifest": self.manifest_path,
            "source_bundle": self.bundle_path,
            "source_lock": self.source_lock_path,
            "checksum": self.checksum_path,
            "bundle_sha256": self.bundle_sha256,
        }
