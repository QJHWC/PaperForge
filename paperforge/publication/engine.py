from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from paperforge.claim_manifest import extract_latex_claim_units
from paperforge.protected_blocks import ProtectedEditTransaction

from .bundle import SourceBundler, verify_source_lock
from .compiler import PublicationCompiler
from .diagnostics import ConstrainedLayoutRepairer, DefaultLayoutDiagnostician
from .invariants import InvariantSnapshot, PublicationInvariantViolation
from .models import (
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
)
from .renderer import PopplerRenderer

PUBLICATION_MANIFEST_SCHEMA = "paperforge.publication.manifest/v1"


class PublicationGateError(RuntimeError):
    pass


def _safe_main(project: Path, main_tex: str | Path) -> Path:
    relative = Path(main_tex)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"unsafe main TeX path: {main_tex}")
    tex_path = (project / relative).resolve()
    if not tex_path.is_relative_to(project) or not tex_path.is_file():
        raise FileNotFoundError(tex_path)
    return tex_path


def _sha256(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative_or_absolute(path: Path | None, project: Path) -> str | None:
    if path is None:
        return None
    try:
        return path.resolve().relative_to(project).as_posix()
    except ValueError:
        return str(path.resolve())


def _relativize_workspace_paths(value: Any, workspace_root: Path) -> Any:
    if isinstance(value, str):
        candidate = Path(value).expanduser()
        if candidate.is_absolute():
            try:
                return candidate.resolve().relative_to(workspace_root).as_posix()
            except ValueError:
                return value
        return value
    if isinstance(value, Mapping):
        return {
            str(key): _relativize_workspace_paths(item, workspace_root)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [
            _relativize_workspace_paths(item, workspace_root)
            for item in value
        ]
    return value


def _deduplicate_issues(issues: list[PublicationIssue]) -> tuple[PublicationIssue, ...]:
    seen: set[tuple[str, str, str]] = set()
    output: list[PublicationIssue] = []
    for issue in issues:
        key = (issue.code, issue.message, issue.severity)
        if key in seen:
            continue
        seen.add(key)
        output.append(issue)
    return tuple(output)


class PublicationEngine:
    def __init__(
        self,
        *,
        compiler: Any | None = None,
        renderer: Any | None = None,
        diagnostician: Any | None = None,
        repairer: Any | None = None,
        bundler: SourceBundler | None = None,
        registry: TemplateProfileRegistry = DEFAULT_TEMPLATE_REGISTRY,
        max_rounds: int = 3,
    ) -> None:
        self.compiler = compiler
        self.renderer = renderer
        self.diagnostician = diagnostician or DefaultLayoutDiagnostician()
        self.repairer = repairer or ConstrainedLayoutRepairer()
        self.bundler = bundler or SourceBundler()
        self.registry = registry
        # PaperFit-style publication work is intentionally bounded.
        self.max_rounds = min(3, max(1, int(max_rounds)))

    def publish(
        self,
        workspace: str | Path,
        *,
        template: str | TemplateProfile | None = None,
        profile: str | TemplateProfile | None = None,
        main_tex: str | Path = "main.tex",
        output_pdf: str | Path | None = None,
        scientific_memory: Any | None = None,
        memory: Any | None = None,
        claim_gate: Mapping[str, Any] | None = None,
        claim_spans: Mapping[str, Mapping[str, Any]] | None = None,
        artifact_dir: str | Path | None = None,
        release_root: str | Path | None = None,
    ) -> PublicationRunResult:
        project = Path(workspace).expanduser().resolve()
        tex_path = _safe_main(project, main_tex)
        selected_memory = self._select_memory(scientific_memory, memory)
        gate = self._claim_gate(selected_memory, claim_gate)
        if not gate.get("passed", False):
            failures = gate.get("failures", ())
            raise PublicationGateError(
                "scientific claim gate failed: "
                + json.dumps(failures, ensure_ascii=False, default=str)
            )

        tex_text = tex_path.read_text(encoding="utf-8")
        selected_profile = self._select_profile(template, profile, tex_text)
        claim_manifest = None
        if selected_memory is not None:
            claim_manifest = dict(selected_memory.claim_manifest(claim_spans or {}))
            claim_manifest.pop("generated_at", None)
            coverage = self._claim_coverage(tex_path, claim_manifest)
            claim_manifest["coverage"] = coverage
            gate["coverage"] = coverage
            if not coverage["passed"]:
                gate["passed"] = False
                gate.setdefault("failures", []).extend(coverage["failures"])
                raise PublicationGateError(
                    "scientific claim coverage failed: "
                    + json.dumps(coverage["failures"], ensure_ascii=False)
                )
        invariant = InvariantSnapshot.capture(
            tex_text,
            scientific_memory=selected_memory,
            claim_spans=claim_spans,
        )

        compiler = self.compiler or PublicationCompiler()
        compiler.validate_project(project, main_tex)
        bibliography_path = project / selected_profile.bibliography_file
        bibliography_baseline = bibliography_path.read_bytes()
        renderer = self.renderer
        if renderer is None:
            renderer = PopplerRenderer(compiler.toolchain)

        artifacts = (
            Path(artifact_dir).expanduser().resolve()
            if artifact_dir is not None
            else project / "dist"
        )
        portable_root = (
            Path(release_root).expanduser().resolve()
            if release_root is not None
            else project
        )
        if (
            project != portable_root
            and portable_root not in project.parents
        ):
            raise ValueError("publication project must be inside release_root")
        if (
            artifacts != portable_root
            and portable_root not in artifacts.parents
        ):
            raise ValueError("publication artifacts must be inside release_root")
        artifacts.mkdir(parents=True, exist_ok=True)
        render_root = project / ".paperforge" / "publication" / "rendered-pages"
        rounds: list[PublicationRound] = []
        completed = False
        final_pdf: Path | None = None
        invariant_verified = False
        source_lock_verified = False

        for round_number in range(1, self.max_rounds + 1):
            compile_result = compiler.compile(
                project,
                main_tex=main_tex,
                output_pdf=output_pdf,
            )
            render_result: RenderResult | None = None
            if compile_result.success and compile_result.pdf_path is not None:
                render_result = renderer.render(
                    compile_result.pdf_path,
                    render_root / f"round-{round_number}",
                )
            diagnosis = self._diagnose(
                compile_result,
                render_result,
                selected_profile,
            )
            publication_round = PublicationRound(
                number=round_number,
                compile_result=compile_result,
                render_result=render_result,
                diagnosis=diagnosis,
            )
            rounds.append(publication_round)

            if diagnosis.clean:
                try:
                    invariant.verify(tex_path.read_text(encoding="utf-8"))
                except PublicationInvariantViolation as exc:
                    issue = PublicationIssue(
                        "INVARIANT_VIOLATION",
                        str(exc),
                        source="publication-engine",
                    )
                    rounds[-1] = replace(
                        publication_round,
                        diagnosis=LayoutDiagnosis((issue,)),
                    )
                    break
                completed = True
                invariant_verified = True
                final_pdf = compile_result.pdf_path
                break

            if round_number >= self.max_rounds:
                break

            context = RepairContext(
                round_number=round_number,
                source_text=tex_path.read_text(encoding="utf-8"),
                tex_path=tex_path,
                profile=selected_profile,
                diagnosis=diagnosis,
                rendered_pages=render_result.pages if render_result is not None else (),
            )
            transaction = ProtectedEditTransaction(tex_path)
            try:
                proposal = self._repair(context)
            except BaseException as exc:
                transaction.rollback()
                bibliography_path.parent.mkdir(parents=True, exist_ok=True)
                bibliography_path.write_bytes(bibliography_baseline)
                if not isinstance(exc, Exception):
                    raise
                issue = PublicationIssue(
                    "REPAIR_FAILED",
                    str(exc),
                    source="publication-engine",
                )
                rounds[-1] = replace(
                    publication_round,
                    diagnosis=LayoutDiagnosis(diagnosis.issues + (issue,)),
                )
                break

            repair_side_effect = (
                tex_path.read_text(encoding="utf-8") != transaction.before_text
                or not bibliography_path.is_file()
                or bibliography_path.read_bytes() != bibliography_baseline
            )
            if repair_side_effect:
                transaction.rollback()
                bibliography_path.parent.mkdir(parents=True, exist_ok=True)
                bibliography_path.write_bytes(bibliography_baseline)
                issue = PublicationIssue(
                    "REPAIR_SIDE_EFFECT",
                    "repairer modified source files outside its returned proposal",
                    source="publication-engine",
                )
                rounds[-1] = replace(
                    publication_round,
                    diagnosis=LayoutDiagnosis(diagnosis.issues + (issue,)),
                )
                break
            if proposal is None:
                break
            if proposal.source_text == context.source_text:
                issue = PublicationIssue(
                    "REPAIR_NOOP",
                    "constrained repair produced no source change",
                    source="publication-engine",
                )
                rounds[-1] = replace(
                    publication_round,
                    diagnosis=LayoutDiagnosis(diagnosis.issues + (issue,)),
                )
                break

            try:
                tex_path.write_text(proposal.source_text, encoding="utf-8")
                transaction.verify()
                invariant.verify(proposal.source_text)
                if (
                    not bibliography_path.is_file()
                    or bibliography_path.read_bytes() != bibliography_baseline
                ):
                    raise PublicationInvariantViolation(
                        "references.bib changed during constrained layout repair"
                    )
            except BaseException as exc:
                transaction.rollback()
                bibliography_path.parent.mkdir(parents=True, exist_ok=True)
                bibliography_path.write_bytes(bibliography_baseline)
                if not isinstance(exc, Exception):
                    raise
                if not isinstance(exc, PublicationInvariantViolation):
                    exc = PublicationInvariantViolation(str(exc))
                issue = PublicationIssue(
                    "REPAIR_REJECTED",
                    str(exc),
                    source="publication-engine",
                )
                rounds[-1] = replace(
                    publication_round,
                    diagnosis=LayoutDiagnosis(diagnosis.issues + (issue,)),
                )
                break
            rounds[-1] = replace(
                publication_round,
                repair_description=proposal.description,
            )

        diagnostics = (
            rounds[-1].diagnosis.issues
            if rounds
            else (
                PublicationIssue(
                    "NO_PUBLICATION_ROUND",
                    "publication engine did not execute a compile round",
                ),
            )
        )
        bundle_result = None
        if completed:
            try:
                bundle_result = self.bundler.build(
                    project,
                    artifacts / "paper-source.zip",
                    profile=selected_profile,
                    main_tex=main_tex,
                    excluded_paths=(final_pdf,) if final_pdf is not None else (),
                )
                verification = verify_source_lock(
                    project,
                    bundle_result.source_lock_path,
                )
                if not verification.valid:
                    completed = False
                    diagnostics = diagnostics + (
                        PublicationIssue(
                            "SOURCE_LOCK_FAILED",
                            "source lock did not verify after bundle creation",
                            source="source-bundle",
                            details={
                                "mismatches": [
                                    {
                                        "path": mismatch.path,
                                        "reason": mismatch.reason,
                                    }
                                    for mismatch in verification.mismatches
                                ]
                            },
                        ),
                    )
                else:
                    source_lock_verified = True
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                completed = False
                diagnostics = diagnostics + (
                    PublicationIssue(
                        "SOURCE_BUNDLE_FAILED",
                        str(exc),
                        source="source-bundle",
                    ),
                )

        final_round = rounds[-1] if rounds else None
        gates = {
            "claim_gate": dict(gate),
            "compile": bool(
                final_round is not None and final_round.compile_result.success
            ),
            "render": bool(
                final_round is not None
                and final_round.render_result is not None
                and final_round.render_result.success
            ),
            "diagnostics": bool(
                final_round is not None and final_round.diagnosis.clean
            ),
            "invariants": invariant_verified,
            "source_lock": source_lock_verified,
        }
        manifest_path = artifacts / "publication.manifest.json"
        manifest = self._manifest(
            project=project,
            profile=selected_profile,
            main_tex=Path(main_tex),
            gate=gate,
            success=completed,
            final_pdf=final_pdf if completed else None,
            rounds=rounds,
            diagnostics=diagnostics,
            bundle_result=bundle_result if completed else None,
            compiler=compiler,
            gates=gates,
            claim_manifest=claim_manifest,
            release_root=portable_root,
        )
        manifest_path.write_text(
            json.dumps(
                manifest,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        return PublicationRunResult(
            success=completed,
            profile=selected_profile.name,
            final_pdf=final_pdf if completed else None,
            rounds=tuple(rounds),
            diagnostics=tuple(diagnostics),
            manifest_path=manifest_path,
            bundle_path=bundle_result.bundle_path if completed and bundle_result else None,
            source_lock_path=(
                bundle_result.source_lock_path if completed and bundle_result else None
            ),
            checksum_path=(
                bundle_result.checksum_path if completed and bundle_result else None
            ),
            bundle_sha256=(
                bundle_result.sha256 if completed and bundle_result else None
            ),
            gates=gates,
        )

    @staticmethod
    def _select_memory(scientific_memory: Any | None, memory: Any | None) -> Any | None:
        if (
            scientific_memory is not None
            and memory is not None
            and scientific_memory is not memory
        ):
            raise ValueError("scientific_memory and memory refer to different objects")
        return scientific_memory if scientific_memory is not None else memory

    @staticmethod
    def _claim_gate(
        memory: Any | None,
        supplied_gate: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        if memory is not None:
            memory_gate = dict(memory.claim_gate(final_publication=True))
            if supplied_gate is None:
                return memory_gate
            supplied = PublicationEngine._validate_supplied_gate(supplied_gate)
            failures = list(memory_gate.get("failures", ()))
            failures.extend(supplied.get("failures", ()))
            return {
                **memory_gate,
                "passed": bool(memory_gate.get("passed"))
                and bool(supplied.get("passed")),
                "failures": failures,
                "supplied_gate": supplied,
            }
        if supplied_gate is not None:
            supplied = PublicationEngine._validate_supplied_gate(supplied_gate)
            return {
                **supplied,
                "passed": False,
                "failures": list(supplied["failures"])
                + [{"reason": "scientific memory is required"}],
            }
        return {
            "passed": False,
            "claim_count": 0,
            "failures": [{"reason": "scientific memory is required"}],
            "mode": "fail-closed",
        }

    @staticmethod
    def _validate_supplied_gate(
        supplied_gate: Mapping[str, Any],
    ) -> dict[str, Any]:
        supplied = dict(supplied_gate)
        if supplied.get("passed") is not True:
            raise PublicationGateError("supplied claim gate must have passed=true")
        claim_count = supplied.get("claim_count")
        if not isinstance(claim_count, int) or isinstance(claim_count, bool) or claim_count < 1:
            raise PublicationGateError("supplied claim gate requires a positive claim_count")
        failures = supplied.get("failures")
        if not isinstance(failures, list | tuple) or failures:
            raise PublicationGateError("supplied claim gate failures must be an empty list")
        return {**supplied, "failures": []}

    @staticmethod
    def _claim_coverage(
        tex_path: Path,
        claim_manifest: Mapping[str, Any],
    ) -> dict[str, Any]:
        units = extract_latex_claim_units(tex_path, relative_file=tex_path.name)
        claims = claim_manifest.get("claims")
        if not isinstance(claims, list):
            return {
                "passed": False,
                "latex_claim_units": len(units),
                "mapped_claims": 0,
                "percent": 0.0,
                "failures": [{"reason": "claim manifest has no claims list"}],
            }
        mapped: set[tuple[str, int, int]] = set()
        for claim in claims:
            if not isinstance(claim, Mapping) or not claim.get("evidence"):
                continue
            span = claim.get("tex_span")
            if not isinstance(span, Mapping):
                continue
            try:
                key = (
                    str(claim.get("text", "")).strip(),
                    int(span["start"]),
                    int(span["end"]),
                )
            except (KeyError, TypeError, ValueError):
                continue
            if str(span.get("file", tex_path.name)) == tex_path.name:
                mapped.add(key)
        failures = [
            {
                "reason": "unmapped LaTeX claim",
                "file": unit.file,
                "line_start": unit.line_start,
                "line_end": unit.line_end,
                "text": unit.text,
            }
            for unit in units
            if (unit.text, unit.line_start, unit.line_end) not in mapped
        ]
        mapped_count = len(units) - len(failures)
        percent = 100.0 if not units else round(100.0 * mapped_count / len(units), 2)
        return {
            "passed": bool(units) and not failures,
            "latex_claim_units": len(units),
            "mapped_claims": mapped_count,
            "percent": percent,
            "failures": failures,
        }

    def _select_profile(
        self,
        template: str | TemplateProfile | None,
        profile: str | TemplateProfile | None,
        tex_text: str,
    ) -> TemplateProfile:
        if template is not None and profile is not None:
            template_profile = self.registry.resolve(template, tex_text=tex_text)
            explicit_profile = self.registry.resolve(profile, tex_text=tex_text)
            if template_profile.name != explicit_profile.name:
                raise ValueError(
                    f"conflicting template/profile: {template_profile.name} != "
                    f"{explicit_profile.name}"
                )
            return template_profile
        return self.registry.resolve(
            template if template is not None else profile,
            tex_text=tex_text,
        )

    def _diagnose(
        self,
        compile_result: CompileResult,
        render_result: RenderResult | None,
        profile: TemplateProfile,
    ) -> LayoutDiagnosis:
        diagnostician = self.diagnostician
        if hasattr(diagnostician, "diagnose"):
            diagnosis = diagnostician.diagnose(compile_result, render_result, profile)
        elif callable(diagnostician):
            diagnosis = diagnostician(compile_result, render_result, profile)
        else:
            raise TypeError("diagnostician must be callable or provide diagnose()")
        if not isinstance(diagnosis, LayoutDiagnosis):
            raise TypeError("diagnostician must return LayoutDiagnosis")

        mandatory = list(compile_result.diagnostics)
        if render_result is not None:
            mandatory.extend(render_result.diagnostics)
        return LayoutDiagnosis(
            _deduplicate_issues(mandatory + list(diagnosis.issues))
        )

    def _repair(self, context: RepairContext) -> RepairProposal | None:
        repairer = self.repairer
        if hasattr(repairer, "repair"):
            proposal = repairer.repair(context)
        elif callable(repairer):
            proposal = repairer(context)
        else:
            raise TypeError("repairer must be callable or provide repair()")
        if proposal is None:
            return None
        if isinstance(proposal, str):
            return RepairProposal(proposal)
        if not isinstance(proposal, RepairProposal):
            raise TypeError("repairer must return str, RepairProposal, or None")
        return proposal

    @staticmethod
    def _manifest(
        *,
        project: Path,
        profile: TemplateProfile,
        main_tex: Path,
        gate: Mapping[str, Any],
        success: bool,
        final_pdf: Path | None,
        rounds: list[PublicationRound],
        diagnostics: tuple[PublicationIssue, ...],
        bundle_result: Any | None,
        compiler: Any,
        gates: Mapping[str, Any],
        claim_manifest: Mapping[str, Any] | None,
        release_root: Path,
    ) -> dict[str, Any]:
        toolchain = getattr(compiler, "toolchain", None)
        toolchain_payload = (
            toolchain.as_dict()
            if toolchain is not None and hasattr(toolchain, "as_dict")
            else {}
        )
        try:
            project_root = project.relative_to(release_root).as_posix() or "."
        except ValueError as exc:
            raise ValueError("publication project leaves release root") from exc
        rounds_payload = _relativize_workspace_paths(
            [round_result.as_dict() for round_result in rounds],
            release_root,
        )
        return {
            "schema": PUBLICATION_MANIFEST_SCHEMA,
            "status": "passed" if success else "failed",
            "project_root": project_root,
            "profile": profile.name,
            "entrypoint": main_tex.as_posix(),
            "bibliography": profile.bibliography_file,
            "dependency_policy": {
                "floating_references_allowed": False,
                "mode": "vendored",
                "network_required": False,
            },
            "claim_gate": dict(gate),
            "claim_manifest": dict(claim_manifest) if claim_manifest is not None else None,
            "gates": dict(gates),
            "round_limit": 3,
            "rounds": rounds_payload,
            "diagnostics": [issue.as_dict() for issue in diagnostics],
            "toolchain": toolchain_payload,
            "artifacts": {
                "pdf": {
                    "path": _relative_or_absolute(final_pdf, release_root),
                    "sha256": _sha256(final_pdf),
                },
                "source_bundle": (
                    {
                        "path": _relative_or_absolute(
                            bundle_result.bundle_path,
                            release_root,
                        ),
                        "sha256": bundle_result.sha256,
                        "checksum_path": _relative_or_absolute(
                            bundle_result.checksum_path,
                            release_root,
                        ),
                        "source_lock_path": _relative_or_absolute(
                            bundle_result.source_lock_path,
                            release_root,
                        ),
                        "source_lock_sha256": bundle_result.source_lock_sha256,
                    }
                    if bundle_result is not None
                    else None
                ),
            },
        }


def publish(
    workspace: str | Path,
    *,
    template: str | TemplateProfile | None = None,
    profile: str | TemplateProfile | None = None,
    memory: Any | None = None,
    scientific_memory: Any | None = None,
    claim_gate: Mapping[str, Any] | None = None,
    engine: PublicationEngine | None = None,
    **kwargs: Any,
) -> PublicationRunResult:
    """Convenience facade used by CLI/service adapters."""

    publication_engine = engine or PublicationEngine()
    return publication_engine.publish(
        workspace,
        template=template,
        profile=profile,
        memory=memory,
        scientific_memory=scientific_memory,
        claim_gate=claim_gate,
        **kwargs,
    )
