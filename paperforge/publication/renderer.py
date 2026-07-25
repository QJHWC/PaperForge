from __future__ import annotations

import contextlib
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .models import CommandResult, PublicationIssue, RenderResult
from .toolchain import Toolchain, ToolchainDiscoveryError, discover_toolchain

Runner = Callable[..., Any]


def _page_sort_key(path: Path) -> tuple[int, str]:
    match = re.search(r"-(\d+)\.png$", path.name)
    return (int(match.group(1)) if match else 0, path.name)


class PopplerRenderer:
    def __init__(
        self,
        toolchain: Toolchain | None = None,
        *,
        runner: Runner = subprocess.run,
        dpi: int = 150,
        timeout: int = 120,
    ) -> None:
        self.toolchain = toolchain or discover_toolchain()
        self.runner = runner
        self.dpi = max(72, int(dpi))
        self.timeout = max(1, int(timeout))

    def _run(self, command: list[str], *, cwd: Path) -> CommandResult:
        try:
            completed = self.runner(
                command,
                cwd=str(cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                check=False,
            )
            return CommandResult(
                command=tuple(command),
                returncode=int(completed.returncode),
                stdout=str(completed.stdout or ""),
                stderr=str(completed.stderr or ""),
            )
        except subprocess.TimeoutExpired as exc:
            return CommandResult(
                command=tuple(command),
                returncode=-1,
                stdout=str(exc.stdout or ""),
                stderr=str(exc.stderr or ""),
                timed_out=True,
            )
        except OSError as exc:
            return CommandResult(
                command=tuple(command),
                returncode=-1,
                stderr=str(exc),
            )

    def render(
        self,
        pdf_path: str | Path,
        output_dir: str | Path,
    ) -> RenderResult:
        pdf = Path(pdf_path).expanduser().resolve()
        output = Path(output_dir).expanduser().resolve()
        output.mkdir(parents=True, exist_ok=True)
        log_path = output / "render.log"
        for page in output.glob("page-*.png"):
            page.unlink()
        with contextlib.suppress(FileNotFoundError):
            log_path.unlink()

        diagnostics: list[PublicationIssue] = []
        commands: list[CommandResult] = []
        try:
            self.toolchain.require_render()
        except ToolchainDiscoveryError as exc:
            diagnostics.append(
                PublicationIssue("TOOLCHAIN_MISSING", str(exc), source="poppler")
            )
            self._write_log(log_path, commands, diagnostics)
            return RenderResult(False, (), 0, log_path, tuple(diagnostics))

        if not pdf.is_file():
            diagnostics.append(
                PublicationIssue("PDF_MISSING", f"PDF does not exist: {pdf}", source="render")
            )
            self._write_log(log_path, commands, diagnostics)
            return RenderResult(False, (), 0, log_path, tuple(diagnostics))

        reported_pages: int | None = None
        if self.toolchain.pdfinfo is not None:
            info_result = self._run([str(self.toolchain.pdfinfo), str(pdf)], cwd=output)
            commands.append(info_result)
            if info_result.returncode == 0:
                match = re.search(
                    r"^Pages:\s*(\d+)\s*$",
                    info_result.stdout,
                    flags=re.MULTILINE | re.IGNORECASE,
                )
                if match:
                    reported_pages = int(match.group(1))

        assert self.toolchain.pdftoppm is not None
        render_result = self._run(
            [
                str(self.toolchain.pdftoppm),
                "-png",
                "-r",
                str(self.dpi),
                str(pdf),
                str(output / "page"),
            ],
            cwd=output,
        )
        commands.append(render_result)
        if render_result.returncode != 0:
            diagnostics.append(
                PublicationIssue(
                    "RENDER_FAILED",
                    f"pdftoppm returned {render_result.returncode}",
                    source="poppler",
                )
            )

        pages = tuple(sorted(output.glob("page-*.png"), key=_page_sort_key))
        if not diagnostics and not pages:
            diagnostics.append(
                PublicationIssue(
                    "RENDER_OUTPUT_MISSING",
                    "pdftoppm returned success but produced no page images",
                    source="poppler",
                )
            )
        if reported_pages is not None and pages and reported_pages != len(pages):
            diagnostics.append(
                PublicationIssue(
                    "PAGE_COUNT_MISMATCH",
                    f"pdfinfo reported {reported_pages} pages but rendered {len(pages)}",
                    source="poppler",
                    details={
                        "pdfinfo_pages": reported_pages,
                        "rendered_pages": len(pages),
                    },
                )
            )

        self._write_log(log_path, commands, diagnostics)
        return RenderResult(
            success=not diagnostics,
            pages=pages if not diagnostics else (),
            page_count=reported_pages if reported_pages is not None else len(pages),
            log_path=log_path,
            diagnostics=tuple(diagnostics),
            commands=tuple(commands),
        )

    @staticmethod
    def _write_log(
        path: Path,
        commands: list[CommandResult],
        diagnostics: list[PublicationIssue],
    ) -> None:
        chunks = ["PaperForge Poppler Renderer"]
        for index, result in enumerate(commands, start=1):
            chunks.extend(
                (
                    "",
                    f"## command {index}",
                    "argv: " + " ".join(result.command),
                    f"returncode: {result.returncode}",
                    "### stdout",
                    result.stdout,
                    "### stderr",
                    result.stderr,
                )
            )
        chunks.extend(("", "## diagnostics"))
        chunks.extend(
            (
                f"{issue.severity.upper()} {issue.code}: {issue.message}"
                for issue in diagnostics
            )
            if diagnostics
            else ("none",)
        )
        path.write_text("\n".join(chunks).rstrip() + "\n", encoding="utf-8")
