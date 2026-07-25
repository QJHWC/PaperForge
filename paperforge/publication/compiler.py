from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .bibliography import (
    BibliographyContract,
    BibliographyContractError,
    validate_single_references_bib,
)
from .models import CommandResult, CompileResult, PublicationIssue
from .toolchain import Toolchain, ToolchainDiscoveryError, discover_toolchain

Runner = Callable[..., Any]

_AUXILIARY_SUFFIXES = (
    ".aux",
    ".bbl",
    ".blg",
    ".fdb_latexmk",
    ".fls",
    ".log",
    ".lof",
    ".lot",
    ".out",
    ".synctex.gz",
    ".toc",
)

_UNRESOLVED_PATTERNS = (
    re.compile(r"(?:Citation|Reference)\s+[`'][^`']+[`'].*undefined", re.IGNORECASE),
    re.compile(r"There were undefined references", re.IGNORECASE),
    re.compile(r"undefined citations", re.IGNORECASE),
    re.compile(r"Please \(re\)run BibTeX", re.IGNORECASE),
)
_OVERFLOW_PATTERN = re.compile(
    r"Overfull \\(?P<box>[hv])box(?:\s*\((?P<amount>[0-9.]+)pt too wide\))?"
    r"(?P<context>[^\n]*)",
    re.IGNORECASE,
)


def _safe_main_path(project: Path, main_tex: str | Path) -> Path:
    relative = Path(main_tex)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"unsafe main TeX path: {main_tex}")
    path = (project / relative).resolve()
    if not path.is_relative_to(project):
        raise ValueError(f"main TeX path leaves project: {main_tex}")
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def _output_path(project: Path, output_pdf: str | Path | None, built_pdf: Path) -> Path:
    if output_pdf is None:
        return built_pdf
    candidate = Path(output_pdf).expanduser()
    if not candidate.is_absolute():
        candidate = project / candidate
    return candidate.resolve()


def _remove_if_present(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


def _has_pdf_header(path: Path) -> bool:
    if path.stat().st_size == 0:
        return False
    with path.open("rb") as handle:
        return handle.read(4) == b"%PDF"


def _diagnose_latex_log(text: str, *, max_overflow_pt: float) -> list[PublicationIssue]:
    diagnostics: list[PublicationIssue] = []
    if any(pattern.search(text) for pattern in _UNRESOLVED_PATTERNS):
        diagnostics.append(
            PublicationIssue(
                code="UNRESOLVED_REFERENCE",
                message="final LaTeX log contains unresolved citations or references",
                source="latex-log",
            )
        )

    overflow_matches = []
    for match in _OVERFLOW_PATTERN.finditer(text):
        amount_text = match.group("amount")
        amount = float(amount_text) if amount_text is not None else None
        if amount is not None and amount <= max_overflow_pt:
            continue
        overflow_matches.append(
            {
                "box": match.group("box").lower(),
                "amount_pt": amount,
                "context": match.group("context").strip(),
            }
        )
    if overflow_matches:
        diagnostics.append(
            PublicationIssue(
                code="OVERFLOW",
                message=f"final LaTeX log contains {len(overflow_matches)} overfull box(es)",
                source="latex-log",
                details={"matches": overflow_matches},
            )
        )
    return diagnostics


class PublicationCompiler:
    def __init__(
        self,
        toolchain: Toolchain | None = None,
        *,
        runner: Runner = subprocess.run,
        timeout: int = 120,
        max_overflow_pt: float = 0.0,
    ) -> None:
        self.toolchain = toolchain or discover_toolchain()
        self.runner = runner
        self.timeout = max(1, int(timeout))
        self.max_overflow_pt = max(0.0, float(max_overflow_pt))

    def validate_project(
        self,
        project_dir: str | Path,
        main_tex: str | Path = "main.tex",
    ) -> BibliographyContract:
        return validate_single_references_bib(project_dir, main_tex)

    def _commands(
        self,
        project: Path,
        tex_path: Path,
        contract: BibliographyContract,
    ) -> tuple[str, list[list[str]]]:
        self.toolchain.require_compile(use_bibtex=contract.uses_bibliography)
        relative_tex = tex_path.relative_to(project).as_posix()
        if self.toolchain.latexmk is not None:
            return (
                "latexmk",
                [
                    [
                        str(self.toolchain.latexmk),
                        "-pdf",
                        "-interaction=nonstopmode",
                        "-halt-on-error",
                        "-file-line-error",
                        relative_tex,
                    ]
                ],
            )

        assert self.toolchain.pdflatex is not None
        pdflatex = [
            str(self.toolchain.pdflatex),
            "-interaction=nonstopmode",
            "-halt-on-error",
            "-file-line-error",
            relative_tex,
        ]
        commands = [pdflatex]
        if contract.uses_bibliography:
            assert self.toolchain.bibtex is not None
            relative_stem = tex_path.relative_to(project).with_suffix("").as_posix()
            commands.append([str(self.toolchain.bibtex), relative_stem])
        commands.extend((pdflatex.copy(), pdflatex.copy()))
        return "pdflatex", commands

    def compile(
        self,
        project_dir: str | Path,
        main_tex: str | Path = "main.tex",
        output_pdf: str | Path | None = None,
    ) -> CompileResult:
        project = Path(project_dir).expanduser().resolve()
        tex_path = _safe_main_path(project, main_tex)
        built_pdf = tex_path.with_suffix(".pdf")
        target_pdf = _output_path(project, output_pdf, built_pdf)
        log_path = project / "publication-compile.log"

        # A stale PDF must never make a failed compile appear successful.
        for path in {built_pdf, target_pdf, log_path}:
            _remove_if_present(path)
        for suffix in _AUXILIARY_SUFFIXES:
            _remove_if_present(tex_path.with_suffix(suffix))

        command_results: list[CommandResult] = []
        diagnostics: list[PublicationIssue] = []
        backend: str | None = None
        try:
            contract = self.validate_project(project, main_tex)
        except BibliographyContractError as exc:
            diagnostics.append(
                PublicationIssue(
                    "BIBLIOGRAPHY_CONTRACT",
                    str(exc),
                    source="references.bib",
                )
            )
            self._write_log(log_path, command_results, diagnostics, "")
            return CompileResult(
                success=False,
                pdf_path=None,
                log_path=log_path,
                diagnostics=tuple(diagnostics),
            )

        try:
            backend, commands = self._commands(project, tex_path, contract)
        except ToolchainDiscoveryError as exc:
            diagnostics.append(
                PublicationIssue("TOOLCHAIN_MISSING", str(exc), source="toolchain")
            )
            self._write_log(log_path, command_results, diagnostics, "")
            return CompileResult(
                success=False,
                pdf_path=None,
                log_path=log_path,
                diagnostics=tuple(diagnostics),
                backend=backend,
            )

        for command in commands:
            try:
                completed = self.runner(
                    command,
                    cwd=str(project),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.timeout,
                    check=False,
                )
                result = CommandResult(
                    command=tuple(str(part) for part in command),
                    returncode=int(completed.returncode),
                    stdout=str(completed.stdout or ""),
                    stderr=str(completed.stderr or ""),
                )
            except subprocess.TimeoutExpired as exc:
                result = CommandResult(
                    command=tuple(str(part) for part in command),
                    returncode=-1,
                    stdout=str(exc.stdout or ""),
                    stderr=str(exc.stderr or ""),
                    timed_out=True,
                )
            except OSError as exc:
                result = CommandResult(
                    command=tuple(str(part) for part in command),
                    returncode=-1,
                    stderr=str(exc),
                )
            command_results.append(result)
            if result.returncode != 0:
                code = "COMMAND_TIMEOUT" if result.timed_out else "COMMAND_FAILED"
                diagnostics.append(
                    PublicationIssue(
                        code=code,
                        message=(
                            f"command returned {result.returncode}: "
                            + " ".join(result.command)
                        ),
                        source="compiler",
                        details={"returncode": result.returncode},
                    )
                )
                break

        latex_log_path = tex_path.with_suffix(".log")
        if latex_log_path.is_file():
            latex_log = latex_log_path.read_text(encoding="utf-8", errors="replace")
        elif command_results:
            last = command_results[-1]
            latex_log = f"{last.stdout}\n{last.stderr}"
        else:
            latex_log = ""

        if not diagnostics:
            diagnostics.extend(
                _diagnose_latex_log(
                    latex_log,
                    max_overflow_pt=self.max_overflow_pt,
                )
            )
        if not diagnostics and not built_pdf.is_file():
            diagnostics.append(
                PublicationIssue(
                    code="PDF_MISSING",
                    message=f"compiler did not produce {built_pdf.name}",
                    source="compiler",
                )
            )
        if not diagnostics and not _has_pdf_header(built_pdf):
            diagnostics.append(
                PublicationIssue(
                    code="PDF_INVALID",
                    message=f"compiler produced an invalid PDF: {built_pdf.name}",
                    source="compiler",
                )
            )

        if diagnostics:
            _remove_if_present(built_pdf)
            if target_pdf != built_pdf:
                _remove_if_present(target_pdf)
            final_pdf = None
        else:
            target_pdf.parent.mkdir(parents=True, exist_ok=True)
            if target_pdf != built_pdf:
                shutil.copy2(built_pdf, target_pdf)
                _remove_if_present(built_pdf)
            final_pdf = target_pdf

        self._write_log(log_path, command_results, diagnostics, latex_log)
        return CompileResult(
            success=not diagnostics,
            pdf_path=final_pdf,
            log_path=log_path,
            diagnostics=tuple(diagnostics),
            commands=tuple(command_results),
            backend=backend,
        )

    @staticmethod
    def _write_log(
        path: Path,
        commands: list[CommandResult],
        diagnostics: list[PublicationIssue],
        latex_log: str,
    ) -> None:
        chunks = ["PaperForge Publication Compiler"]
        for index, result in enumerate(commands, start=1):
            chunks.extend(
                (
                    "",
                    f"## command {index}",
                    "argv: " + " ".join(result.command),
                    f"returncode: {result.returncode}",
                    f"timed_out: {str(result.timed_out).lower()}",
                    "### stdout",
                    result.stdout,
                    "### stderr",
                    result.stderr,
                )
            )
        chunks.extend(("", "## diagnostics"))
        if diagnostics:
            chunks.extend(
                f"{issue.severity.upper()} {issue.code}: {issue.message}"
                for issue in diagnostics
            )
        else:
            chunks.append("none")
        chunks.extend(("", "## final latex log", latex_log))
        path.write_text("\n".join(chunks).rstrip() + "\n", encoding="utf-8")
