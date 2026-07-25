#!/usr/bin/env python3
"""Compile main.tex for a paper project."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared"))

from paper_utils import check_latex_available  # noqa: E402


def run(cmd: list[str], cwd: Path) -> int:
    result = subprocess.run(cmd, cwd=str(cwd))
    return result.returncode


def parse_log_warnings(log_path: Path) -> dict[str, list[str]]:
    """Parse main.log for LaTeX warnings, return categorized lists."""
    warnings: dict[str, list[str]] = {
        "overfull_hbox": [],
        "underfull_hbox": [],
        "undefined_citations": [],
        "undefined_references": [],
        "multiply_defined": [],
        "missing_files": [],
    }
    if not log_path.exists():
        return warnings
    text = log_path.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        if re.search(r"Overfull \\hbox .+ in paragraph at lines? (\d+)", line):
            warnings["overfull_hbox"].append(line.strip())
        elif re.search(r"Underfull \\hbox .+ in paragraph at lines? (\d+)", line):
            warnings["underfull_hbox"].append(line.strip())
        elif re.search(r"Citation [`']([^']+)['\"] on page (\d+) undefined", line) or \
             re.search(r"LaTeX Warning: Citation [`']([^']+)['\"] on page", line):
            warnings["undefined_citations"].append(line.strip())
        elif re.search(r"Reference [`']([^']+)['\"] on page (\d+) undefined", line):
            warnings["undefined_references"].append(line.strip())
        elif re.search(r"Label [`']([^']+)['\"] multiply defined", line):
            warnings["multiply_defined"].append(line.strip())
        elif re.search(r"File [`']([^']+)['\"] not found", line):
            warnings["missing_files"].append(line.strip())
    return warnings


def report_warnings(warnings: dict[str, list[str]]) -> int:
    """Print warning summary, return total critical warning count."""
    critical_categories = [
        "overfull_hbox",
        "undefined_citations",
        "undefined_references",
        "multiply_defined",
        "missing_files",
    ]
    critical = 0
    for category, items in warnings.items():
        if not items:
            continue
        label = category.replace("_", " ").title()
        severity = "CRITICAL" if category in critical_categories else "INFO"
        print(f"\n[{severity}] {label}: {len(items)}")
        for item in items[:20]:
            print(f"  - {item}")
        if len(items) > 20:
            print(f"  ... and {len(items) - 20} more")
        if category in critical_categories:
            critical += len(items)
    if critical == 0:
        print("\nNo critical LaTeX warnings found.")
    else:
        print(f"\nTotal critical warnings: {critical}")
    return critical


def parse_total_pages(log_path: Path) -> int | None:
    if not log_path.exists():
        return None
    text = log_path.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"Output written on .*?\((\d+)\s+pages?", text)
    if not match:
        return None
    return int(match.group(1))


def parse_label_page(aux_path: Path, label: str) -> int | None:
    if not aux_path.exists():
        return None
    text = aux_path.read_text(encoding="utf-8", errors="replace")

    def parse_braced_group(content: str, start_idx: int) -> tuple[str, int] | None:
        if start_idx >= len(content) or content[start_idx] != "{":
            return None
        depth = 0
        i = start_idx + 1
        group_start = i
        while i < len(content):
            ch = content[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                if depth == 0:
                    return content[group_start:i], i + 1
                depth -= 1
            i += 1
        return None

    label_prefix = f"\\newlabel{{{label}}}"
    for line in text.splitlines():
        if not line.startswith(label_prefix):
            continue
        brace_start = line.find("{", len(label_prefix))
        if brace_start == -1:
            continue
        outer = parse_braced_group(line, brace_start)
        if outer is None:
            continue
        outer_content, _ = outer

        idx = 0
        first = parse_braced_group(outer_content, idx)
        if first is None:
            continue
        _, idx = first
        while idx < len(outer_content) and outer_content[idx].isspace():
            idx += 1
        second = parse_braced_group(outer_content, idx)
        if second is None:
            continue
        page_str, _ = second
        page_str = page_str.strip()
        if page_str.isdigit():
            return int(page_str)
    return None


def report_page_counts(project_dir: Path, label: str) -> None:
    log_path = project_dir / "main.log"
    aux_path = project_dir / "main.aux"

    total_pages = parse_total_pages(log_path)
    if total_pages is None:
        print("warning: could not read total page count from main.log", file=sys.stderr)
        return

    bib_start_page = parse_label_page(aux_path, label)
    if bib_start_page is None:
        print(
            f"warning: could not find label '{label}' in main.aux; "
            "add a label at bibliography start to enable main-text page counting",
            file=sys.stderr,
        )
        return

    main_text_pages_excl_bib_start = max(bib_start_page - 1, 0)
    reference_pages_incl_bib_start = max(total_pages - main_text_pages_excl_bib_start, 0)

    main_text_pages_incl_bib_start = bib_start_page
    reference_pages_excl_bib_start = max(total_pages - main_text_pages_incl_bib_start, 0)

    print("\nPage count report:")
    print(f"  Total pages (incl. references): {total_pages}")
    print(f"  References start page (label '{label}'): {bib_start_page}")
    print(f"  Main text pages (exclude ref-start page): {main_text_pages_excl_bib_start}")
    print(f"  Reference pages (include ref-start page): {reference_pages_incl_bib_start}")
    print(f"  Main text pages (include ref-start page): {main_text_pages_incl_bib_start}")
    print(f"  Reference pages (exclude ref-start page): {reference_pages_excl_bib_start}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Compile main.tex (pdflatex/bibtex or latexmk).")
    parser.add_argument(
        "--project-dir",
        default=".",
        help="Project directory containing main.tex and ref.bib.",
    )
    parser.add_argument(
        "--report-page-counts",
        action="store_true",
        help="Print total and main-text page counts (requires a bibliography-start label).",
    )
    parser.add_argument(
        "--references-start-label",
        default="ReferencesStart",
        help="Label name recorded at bibliography start (default: ReferencesStart).",
    )
    parser.add_argument(
        "--check-warnings",
        action="store_true",
        help="Parse main.log for Overfull/Undefined/Missing warnings after compilation.",
    )
    parser.add_argument(
        "--fail-on-warnings",
        action="store_true",
        help="Return exit code 2 when critical LaTeX warnings are found.",
    )
    args = parser.parse_args()

    project_dir = Path(args.project_dir).resolve()
    tex_path = project_dir / "main.tex"

    if not tex_path.exists():
        print(f"error: main.tex not found in {project_dir}", file=sys.stderr)
        return 1

    latex_info = check_latex_available()
    if not latex_info["available"]:
        print("error: LaTeX tools not available (pdflatex + bibtex required).", file=sys.stderr)
        return 1

    latexmk = latex_info.get("latexmk")
    check = args.check_warnings or args.fail_on_warnings

    if latexmk:
        cmd = [
            latexmk,
            "-pdf",
            "-interaction=nonstopmode",
            "-halt-on-error",
            "main.tex",
        ]
        code = run(cmd, project_dir)
        if code == 0 and args.report_page_counts:
            report_page_counts(project_dir, args.references_start_label)
        if code == 0 and check:
            log_warnings = parse_log_warnings(project_dir / "main.log")
            critical_count = report_warnings(log_warnings)
            if args.fail_on_warnings and critical_count > 0:
                return 2
        return code

    # Fallback: pdflatex + bibtex
    steps = [
        [latex_info["pdflatex"], "-interaction=nonstopmode", "-halt-on-error", "main.tex"],
        [latex_info["bibtex"], "main"],
        [latex_info["pdflatex"], "-interaction=nonstopmode", "-halt-on-error", "main.tex"],
        [latex_info["pdflatex"], "-interaction=nonstopmode", "-halt-on-error", "main.tex"],
    ]

    for cmd in steps:
        code = run(cmd, project_dir)
        if code != 0:
            return code

    if args.report_page_counts:
        report_page_counts(project_dir, args.references_start_label)

    if check:
        log_warnings = parse_log_warnings(project_dir / "main.log")
        critical_count = report_warnings(log_warnings)
        if args.fail_on_warnings and critical_count > 0:
            return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
