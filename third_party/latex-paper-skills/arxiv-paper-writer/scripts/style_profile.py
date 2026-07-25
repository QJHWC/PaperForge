#!/usr/bin/env python3
"""Manage venue style profiles and draft conformance checks."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared"))

from paper_utils import (  # noqa: E402
    build_default_style_profile,
    count_citations,
    extract_section_events,
    get_style_profile_path,
    load_paper_config,
    read_simple_yaml_file,
    strip_latex_markup,
    write_simple_yaml_file,
)


def abstract_text(content: str) -> str:
    """Extract the abstract block."""
    match = re.search(r"\\begin\{abstract\}(.*?)\\end\{abstract\}", content, re.DOTALL)
    if match is None:
        return ""
    return strip_latex_markup(match.group(1))


def count_figures_tables(content: str) -> tuple[int, int]:
    """Count figure and table environments."""
    figure_count = len(re.findall(r"\\begin\{figure\*?\}", content))
    table_count = len(re.findall(r"\\begin\{table\*?\}", content))
    return figure_count, table_count


def cmd_init_profile(args: argparse.Namespace) -> int:
    config = load_paper_config(args.project_dir)
    profile_path = get_style_profile_path(args.project_dir)
    if profile_path.exists() and not args.force:
        print(f"error: style profile already exists: {profile_path}", file=sys.stderr)
        return 1

    profile = build_default_style_profile(config)
    write_simple_yaml_file(profile_path, profile)
    print(f"Created style profile: {profile_path}")
    return 0


def cmd_check_draft(args: argparse.Namespace) -> int:
    profile_path = get_style_profile_path(args.project_dir)
    if not profile_path.exists():
        print(f"error: style profile not found: {profile_path}", file=sys.stderr)
        return 1

    tex_path = args.project_dir / "main.tex"
    if not tex_path.exists():
        print(f"error: main.tex not found: {tex_path}", file=sys.stderr)
        return 1

    profile = read_simple_yaml_file(profile_path)
    content = tex_path.read_text(encoding="utf-8")

    abstract_words = len(re.findall(r"[A-Za-z0-9]+", abstract_text(content)))
    sections = [event["title"] for event in extract_section_events(content) if event["level"] == "section"]
    figures, tables = count_figures_tables(content)
    citation_info = count_citations(tex_path)

    abstract_range = profile.get("abstract_word_range") or {}
    abstract_min = int(abstract_range.get("min", 150))
    abstract_max = int(abstract_range.get("max", 250))
    style_sections = [str(item) for item in (profile.get("canonical_section_order") or [])]
    figure_targets = profile.get("figure_table_density_target") or {}
    min_figures_tables = int(figure_targets.get("min_total", 5))

    print(f"Venue: {profile.get('venue')}")
    print(f"Abstract words: {abstract_words} (target {abstract_min}-{abstract_max})")
    print(f"Sections: {', '.join(sections) if sections else 'N/A'}")
    print(f"Figures + tables: {figures + tables} (min target {min_figures_tables})")
    print(f"Citations: total={citation_info['total']} unique={citation_info['unique']}")

    failures = 0
    if not (abstract_min <= abstract_words <= abstract_max):
        failures += 1
        print("- WARN: abstract length is outside the configured range")

    if style_sections:
        section_prefix = sections[: len(style_sections)]
        if section_prefix != style_sections[: len(section_prefix)]:
            failures += 1
            print("- WARN: section order diverges from the configured canonical order")

    if (figures + tables) < min_figures_tables:
        failures += 1
        print("- WARN: figure/table count is below the configured minimum")

    return 1 if failures and args.fail_on_deviation else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create and audit venue style profiles.")
    parser.add_argument("--project-dir", required=True, help="Paper/project directory.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init-profile", help="Create notes/style-profile.yaml from paper.config.yaml.")
    p_init.add_argument("--force", action="store_true", help="Overwrite an existing style profile.")
    p_init.set_defaults(fn=cmd_init_profile)

    p_check = sub.add_parser("check-draft", help="Check main.tex against notes/style-profile.yaml.")
    p_check.add_argument("--fail-on-deviation", action="store_true", help="Return a non-zero exit code when checks deviate.")
    p_check.set_defaults(fn=cmd_check_draft)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    args.project_dir = Path(args.project_dir).expanduser().resolve()
    return int(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
