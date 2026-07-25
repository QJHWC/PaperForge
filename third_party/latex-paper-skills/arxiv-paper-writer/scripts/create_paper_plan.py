#!/usr/bin/env python3
"""Create a paper plan and/or issues CSV for an IEEE paper project."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared"))

from paper_utils import (  # noqa: E402
    build_issues_filename,
    build_plan_filename,
    check_latex_available,
    ensure_paper_config,
    format_yaml_value,
    load_paper_config,
    now_iso,
    now_timestamp,
    slugify,
    validate_slug,
    validate_timestamp,
)

_SKILL_ASSETS = Path(__file__).resolve().parents[1] / "assets"


def read_template(template_name: str) -> str:
    """Read a template file from assets."""
    template_path = _SKILL_ASSETS / template_name
    if not template_path.exists():
        raise FileNotFoundError(f"Template not found: {template_path}")
    return template_path.read_text(encoding="utf-8")


def replace_placeholders(
    content: str,
    *,
    topic: str,
    timestamp: str,
    slug: str,
    latex_available: bool,
    config: dict,
    workflow_mode: str,
) -> str:
    """Replace placeholders in template content."""
    preferred_venues = config.get("preferred_venues") or []
    preferred_text = ", ".join(str(item) for item in preferred_venues) if preferred_venues else "TBD"
    content = content.replace("<paper topic>", topic)
    content = content.replace("<topic>", topic)
    content = content.replace("<ISO8601 timestamp>", now_iso())
    content = content.replace("<YYYY-MM-DD_HH-mm-ss>", timestamp)
    content = content.replace("<slug>", slug)
    content = content.replace("<true|false>", "true" if latex_available else "false")
    content = content.replace("<workflow mode>", workflow_mode)
    content = content.replace("<target venue>", str(config.get("target_venue") or "TBD"))
    content = content.replace("<preferred venues>", preferred_text)
    content = content.replace("<style mode>", str(config.get("style_mode") or "neutral"))
    return content


def kickoff_gate_confirmed(plan_path: Path) -> bool:
    """Return True if the plan indicates the kickoff gate is confirmed."""
    if not plan_path.exists():
        return False
    text = plan_path.read_text(encoding="utf-8")
    for line in text.splitlines():
        if re.search(r"^\s*-\s*\[\s*[xX]\s*\]\s*User confirmed scope \+ outline", line):
            return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a paper plan or issues CSV for an IEEE paper project.")
    parser.add_argument("--topic", required=True, help="Paper topic description.")
    parser.add_argument(
        "--stage",
        default="plan",
        choices=["plan", "issues", "outline"],
        help="What to create: plan | issues | outline (default: plan).",
    )
    parser.add_argument(
        "--complexity",
        default="medium",
        choices=["simple", "medium", "complex"],
        help="Plan complexity: simple|medium|complex.",
    )
    parser.add_argument(
        "--timestamp",
        help="Optional timestamp override (YYYY-MM-DD_HH-mm-ss). Required for --stage issues.",
    )
    parser.add_argument(
        "--slug",
        help="Optional slug override for filenames (lower-case, hyphen-delimited).",
    )
    parser.add_argument(
        "--output-dir",
        default=".",
        help="Output directory for plan and issues files (default: current directory).",
    )
    parser.add_argument(
        "--check-latex",
        action="store_true",
        help="Check if LaTeX is available and set latex_available accordingly.",
    )
    parser.add_argument(
        "--with-literature-notes",
        action="store_true",
        help="Create notes/literature-notes.md to track paper summaries per citation key.",
    )
    parser.add_argument("--target-venue", default="", help="Optional target venue or journal.")
    parser.add_argument(
        "--preferred-venue",
        action="append",
        default=[],
        help="Repeatable preferred venue/journal hint.",
    )
    parser.add_argument(
        "--style-mode",
        default="neutral",
        choices=["neutral", "target_venue"],
        help="Style profile mode to seed in paper.config.yaml.",
    )
    parser.add_argument(
        "--style-anchor-paper",
        action="append",
        default=[],
        help="Repeatable style anchor paper identifier/URL.",
    )
    args = parser.parse_args()

    topic = args.topic.strip()
    if not topic:
        print("error: Topic cannot be empty.", file=sys.stderr)
        return 1

    slug = args.slug.strip() if args.slug else slugify(topic)
    try:
        validate_slug(slug)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if args.timestamp:
        timestamp = args.timestamp.strip()
        try:
            validate_timestamp(timestamp)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
    else:
        if args.stage == "issues":
            print("error: --timestamp is required when using --stage issues.", file=sys.stderr)
            return 1
        timestamp = now_timestamp()

    latex_info = check_latex_available()
    latex_available = latex_info["available"]

    if args.check_latex:
        print(f"LaTeX available: {latex_available}")
        if latex_available:
            print(f"  pdflatex: {latex_info['pdflatex']}")
            print(f"  bibtex: {latex_info['bibtex']}")
            if latex_info["latexmk"]:
                print(f"  latexmk: {latex_info['latexmk']}")
            print(f"  Recommended: {latex_info['recommended']}")
        else:
            print("  LaTeX not found. Paper will be created without compilation.")
            print("  User can compile later with Overleaf or local LaTeX installation.")

    try:
        issues_template = read_template("paper-issues-template.csv")
        plan_template = read_template("paper-plan-template.md") if args.stage in {"plan", "outline"} else None
        literature_template = (
            read_template("literature-notes-template.md")
            if args.with_literature_notes and args.stage == "issues"
            else None
        )
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    output_dir = Path(args.output_dir).resolve()
    workflow_mode = "outline-only" if args.stage == "outline" else "standard"
    _, config, _ = ensure_paper_config(
        project_dir=output_dir,
        topic=topic,
        workflow_mode=workflow_mode,
        target_venue=args.target_venue,
        preferred_venues=args.preferred_venue,
        style_mode=args.style_mode,
        style_anchor_papers=args.style_anchor_paper,
    )
    config = load_paper_config(output_dir)

    plan_content = (
        replace_placeholders(
            plan_template,
            topic=topic,
            timestamp=timestamp,
            slug=slug,
            latex_available=latex_available,
            config=config,
            workflow_mode=workflow_mode,
        )
        if plan_template
        else None
    )
    issues_content = issues_template
    literature_content = (
        replace_placeholders(
            literature_template,
            topic=topic,
            timestamp=timestamp,
            slug=slug,
            latex_available=latex_available,
            config=config,
            workflow_mode=workflow_mode,
        )
        if literature_template
        else None
    )

    plan_dir = output_dir / "plan"
    issues_dir = output_dir / "issues"
    plan_filename = build_plan_filename(timestamp, slug)
    issues_filename = build_issues_filename(timestamp, slug)
    plan_path = plan_dir / plan_filename
    issues_path = issues_dir / issues_filename
    literature_dir = output_dir / "notes"
    literature_path = literature_dir / "literature-notes.md"

    if args.stage == "issues" and not plan_path.exists():
        print(f"error: Plan not found (create the plan first): {plan_path}", file=sys.stderr)
        return 1
    if args.stage in {"plan", "outline"} and plan_path.exists():
        print(f"error: Plan already exists: {plan_path}", file=sys.stderr)
        return 1
    if args.stage == "issues" and issues_path.exists():
        print(f"error: Issues file already exists: {issues_path}", file=sys.stderr)
        return 1
    if literature_content and literature_path.exists():
        print(f"error: Literature notes already exists: {literature_path}", file=sys.stderr)
        return 1
    if args.stage == "issues" and not kickoff_gate_confirmed(plan_path):
        print(
            "error: Kickoff gate is not confirmed in the plan. "
            "Edit the plan and check the box '- [x] User confirmed scope + outline in chat' before creating issues.",
            file=sys.stderr,
        )
        return 1

    mode_value = "paper-outline" if args.stage == "outline" else "paper-plan"
    frontmatter = (
        "---\n"
        f"mode: {format_yaml_value(mode_value)}\n"
        f"topic: {format_yaml_value(topic)}\n"
        f"timestamp: {format_yaml_value(timestamp)}\n"
        f"slug: {format_yaml_value(slug)}\n"
        f"created_at: {format_yaml_value(now_iso())}\n"
        f"complexity: {format_yaml_value(args.complexity)}\n"
        f"latex_available: {str(latex_available).lower()}\n"
        f"workflow_mode: {format_yaml_value(workflow_mode)}\n"
        "---\n\n"
    )

    if plan_content and plan_content.lstrip().startswith("---"):
        first_dash = plan_content.find("---")
        second_dash = plan_content.find("---", first_dash + 3)
        if second_dash != -1:
            plan_content = plan_content[second_dash + 3 :].lstrip()

    if args.stage in {"plan", "outline"}:
        plan_dir.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(frontmatter + (plan_content or "") + "\n", encoding="utf-8")
        print(f"Created plan: {plan_path}")
    if args.stage == "issues":
        issues_dir.mkdir(parents=True, exist_ok=True)
        issues_path.write_text(issues_content, encoding="utf-8")
        print(f"Created issues: {issues_path}")
    if literature_content:
        literature_dir.mkdir(parents=True, exist_ok=True)
        literature_path.write_text(literature_content.rstrip() + "\n", encoding="utf-8")
        print(f"Created literature notes: {literature_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
