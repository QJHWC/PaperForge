#!/usr/bin/env python3
"""Issue execution helpers for section scaffolding, citation sync, and QA."""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared"))

from arxiv_registry import connect, ensure_bibtex, ensure_citation_key, ensure_initialized, ensure_work, init_schema, rewrite_bibtex_key  # noqa: E402
from citation_policy import normalize_section_lookup_map  # noqa: E402
from compile_paper import parse_label_page, parse_total_pages  # noqa: E402
from paper_utils import (  # noqa: E402
    build_default_style_profile,
    extract_section_events,
    get_style_profile_path,
    load_paper_config,
    normalize_text,
    parse_bibtex_entries,
    read_simple_yaml_file,
)
from style_profile import count_figures_tables  # noqa: E402


CURRENT_COLUMNS = [
    "ID",
    "Phase",
    "Title",
    "Section_Path",
    "Description",
    "Source_Policy",
    "Target_Citations",
    "Visualization",
    "Acceptance",
    "Status",
    "Verified_Citations",
    "Notes",
]

LEGACY_COLUMNS = [
    "ID",
    "Phase",
    "Title",
    "Description",
    "Target_Citations",
    "Visualization",
    "Acceptance",
    "Status",
    "Verified_Citations",
    "Notes",
]

EMPIRICAL_COLUMNS = [
    "ID",
    "Phase",
    "Title",
    "Section_Path",
    "Claim_ID",
    "Evidence_Type",
    "Experiment_ID",
    "Result_Status",
    "Description",
    "Source_Policy",
    "Target_Citations",
    "Visualization",
    "Acceptance",
    "Status",
    "Verified_Citations",
    "Depends_On",
    "Must_Verify",
    "Notes",
]

LEVEL_COMMAND = {
    1: "section",
    2: "subsection",
    3: "subsubsection",
}

LEVEL_RANK = {
    "section": 1,
    "subsection": 2,
    "subsubsection": 3,
}

PLACEHOLDER_RULES = [
    ("angle-bracket placeholder", re.compile(r"<[^>]+>")),
    ("template bracket placeholder", re.compile(r"\[(?:Your|Keyword|Author|Institution)[^\]]*\]", re.IGNORECASE)),
    ("TODO marker", re.compile(r"\bTODO\b")),
    ("TBD marker", re.compile(r"\bTBD\b")),
]


def fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


def resolve_issues_path(project_dir: Path, issues_path: Path | None) -> Path:
    if issues_path is not None:
        path = issues_path.expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"issues file not found: {path}")
        return path

    issues_dir = project_dir / "issues"
    if not issues_dir.exists():
        raise FileNotFoundError(f"issues directory not found: {issues_dir}")
    candidates = sorted(path for path in issues_dir.glob("*.csv") if path.is_file())
    if not candidates:
        raise FileNotFoundError(f"no issues CSV found in: {issues_dir}")
    return candidates[-1]


def read_issue_table(path: Path) -> tuple[list[str], list[dict[str, str]], str]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"empty issues CSV: {path}")
        header = [field.strip() for field in reader.fieldnames]
        rows: list[dict[str, str]] = []
        for row in reader:
            clean = {key: (value or "") for key, value in row.items() if key is not None}
            if any(value.strip() for value in clean.values()):
                rows.append(clean)

    if header == CURRENT_COLUMNS:
        schema = "current"
    elif header == EMPIRICAL_COLUMNS:
        schema = "empirical"
    elif header == LEGACY_COLUMNS:
        schema = "legacy"
    else:
        missing_current = [column for column in CURRENT_COLUMNS if column not in header]
        schema = "current" if not missing_current else "unknown"
    return header, rows, schema


def write_issue_table(path: Path, header: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=header)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in header})


def parse_int(value: str, default: int = 0) -> int:
    try:
        return int((value or "").strip())
    except ValueError:
        return default


def detect_placeholders(content: str) -> list[str]:
    findings: list[str] = []
    for label, pattern in PLACEHOLDER_RULES:
        if pattern.search(content):
            findings.append(label)
    return findings


def build_section_ranges(content: str) -> dict[str, dict[str, Any]]:
    events = extract_section_events(content)
    ranges: dict[str, dict[str, Any]] = {}
    for index, event in enumerate(events):
        current_level = LEVEL_RANK.get(str(event["level"]), 99)
        end = len(content)
        for next_event in events[index + 1 :]:
            next_level = LEVEL_RANK.get(str(next_event["level"]), 99)
            if next_level <= current_level:
                end = int(next_event["start"])
                break
        normalized_path = str(event["normalized_path"])
        ranges[normalized_path] = {
            **event,
            "end": end,
            "content": content[int(event["start"]):end],
        }

    # IEEE-style templates render the abstract as an environment rather than a section.
    # Treat it as a pseudo-section so Writing issues can target it via Section_Path=Abstract.
    abstract_match = re.search(r"\\begin\{abstract\}.*?\\end\{abstract\}", content, flags=re.DOTALL)
    if abstract_match:
        ranges.setdefault(
            "abstract",
            {
                "level": "section",
                "title": "Abstract",
                "normalized_path": "abstract",
                "start": abstract_match.start(),
                "end": abstract_match.end(),
                "content": content[abstract_match.start():abstract_match.end()],
            },
        )
    return ranges


def load_style_profile(project_dir: Path) -> dict[str, Any]:
    config = load_paper_config(project_dir)
    profile_path = get_style_profile_path(project_dir)
    if profile_path.exists():
        return read_simple_yaml_file(profile_path)
    return build_default_style_profile(config)


def page_report(project_dir: Path, references_start_label: str) -> dict[str, int] | None:
    log_path = project_dir / "main.log"
    aux_path = project_dir / "main.aux"
    if not log_path.exists():
        return None
    total_pages = parse_total_pages(log_path)
    if total_pages is None:
        return None
    report: dict[str, int] = {"total_pages": total_pages}
    bib_start_page = parse_label_page(aux_path, references_start_label)
    if bib_start_page is not None:
        report["references_start_page"] = bib_start_page
        report["main_text_pages_excluding_ref_start"] = max(bib_start_page - 1, 0)
    return report


def severity_rank(status: str) -> int:
    return {"PASS": 0, "WARN": 1, "FAIL": 2}[status]


def combine_status(current: str, candidate: str) -> str:
    return candidate if severity_rank(candidate) > severity_rank(current) else current


def build_issue_analysis(project_dir: Path, issues_path: Path) -> dict[str, Any]:
    header, rows, schema = read_issue_table(issues_path)
    tex_path = project_dir / "main.tex"
    bib_path = project_dir / "ref.bib"
    if not tex_path.exists():
        raise FileNotFoundError(f"main.tex not found in {project_dir}")
    if not bib_path.exists():
        raise FileNotFoundError(f"ref.bib not found in {project_dir}")

    tex_content = tex_path.read_text(encoding="utf-8")
    bib_content = bib_path.read_text(encoding="utf-8")
    section_ranges = build_section_ranges(tex_content)
    citation_map = normalize_section_lookup_map(tex_content)
    bib_keys = {entry["key"] for entry in parse_bibtex_entries(bib_content)}
    figures, tables = count_figures_tables(tex_content)
    profile = load_style_profile(project_dir)
    min_figures_tables = int((profile.get("figure_table_density_target") or {}).get("min_total", 5))
    whole_doc_placeholders = detect_placeholders(tex_content)

    writing_rows: list[dict[str, Any]] = []
    skipped_rows: list[str] = []
    failures = 0
    warnings = 0

    for row in rows:
        if row.get("Phase") != "Writing":
            continue
        issue_id = row.get("ID", "?")
        if "Section_Path" not in header:
            skipped_rows.append(issue_id)
            continue

        raw_section_path = row.get("Section_Path", "")
        normalized_path = normalize_text(raw_section_path)
        section_info = section_ranges.get(normalized_path)
        section_exists = section_info is not None
        cited_keys = sorted(citation_map.get(normalized_path, set()))
        verified_keys = sorted(key for key in cited_keys if key in bib_keys)
        placeholders = detect_placeholders(str(section_info["content"])) if section_info else []
        target_citations = parse_int(row.get("Target_Citations", "0"))
        csv_verified = parse_int(row.get("Verified_Citations", "0"))

        status = "PASS"
        findings: list[str] = []
        if not section_exists:
            status = combine_status(status, "FAIL")
            findings.append("section missing from main.tex")
        if section_exists and not cited_keys:
            status = combine_status(status, "WARN")
            findings.append("no citations detected in section subtree")
        if placeholders:
            severity = "FAIL" if (row.get("Status") or "").strip() == "DONE" else "WARN"
            status = combine_status(status, severity)
            findings.append("placeholders remain: " + ", ".join(placeholders))
        if csv_verified != len(verified_keys):
            status = combine_status(status, "WARN")
            findings.append(f"Verified_Citations={csv_verified} but actual verified keys={len(verified_keys)}")
        if (row.get("Status") or "").strip() == "DONE" and len(verified_keys) < target_citations:
            status = combine_status(status, "FAIL")
            findings.append(
                f"DONE issue below target citations ({len(verified_keys)} < {target_citations})"
            )

        if status == "FAIL":
            failures += 1
        elif status == "WARN":
            warnings += 1

        writing_rows.append(
            {
                "issue_id": issue_id,
                "title": row.get("Title", ""),
                "section_path": raw_section_path,
                "status": status,
                "findings": findings,
                "section_exists": section_exists,
                "cited_keys": cited_keys,
                "verified_keys": verified_keys,
                "target_citations": target_citations,
                "csv_verified": csv_verified,
                "row": row,
                "placeholders": placeholders,
            }
        )

    document_findings: list[tuple[str, str]] = []
    if whole_doc_placeholders:
        document_findings.append(("WARN", "document still contains placeholders: " + ", ".join(whole_doc_placeholders)))
        warnings += 1
    if figures + tables < min_figures_tables:
        document_findings.append(
            ("WARN", f"figures+tables below target ({figures + tables} < {min_figures_tables})")
        )
        warnings += 1

    return {
        "header": header,
        "rows": rows,
        "schema": schema,
        "writing_rows": writing_rows,
        "skipped_rows": skipped_rows,
        "failures": failures,
        "warnings": warnings,
        "document_findings": document_findings,
        "figures": figures,
        "tables": tables,
        "bib_keys": bib_keys,
        "page_report": page_report(project_dir, "ReferencesStart"),
    }


def print_analysis_summary(issues_path: Path, analysis: dict[str, Any]) -> None:
    print(f"Issues file: {issues_path}")
    print(f"Schema: {analysis['schema']}")
    print(f"Figures + tables: {analysis['figures'] + analysis['tables']}")
    print(f"BibTeX keys: {len(analysis['bib_keys'])}")
    page_info = analysis.get("page_report")
    if page_info:
        print(f"Total pages: {page_info['total_pages']}")
        if "main_text_pages_excluding_ref_start" in page_info:
            print(
                "Main-text pages (exclude reference-start page): "
                f"{page_info['main_text_pages_excluding_ref_start']}"
            )

    if analysis["schema"] in ("legacy", "unknown"):
        print("- WARN: issues CSV uses legacy or unknown schema; section-path checks and sync are limited")

    for status, message in analysis["document_findings"]:
        print(f"- DOC [{status}] {message}")

    if not analysis["writing_rows"]:
        print("No writing issues found.")
        return

    for item in analysis["writing_rows"]:
        print(
            f"- {item['issue_id']} [{item['status']}] section={item['section_path']} "
            f"exists={'yes' if item['section_exists'] else 'no'} cited={len(item['cited_keys'])} "
            f"verified={len(item['verified_keys'])} csv={item['csv_verified']} target={item['target_citations']}"
        )
        for finding in item["findings"]:
            print(f"    - {finding}")


def render_issue_skeleton_text(row: dict[str, str]) -> str:
    raw_path = row.get("Section_Path", "")
    path_parts = [part.strip() for part in raw_path.split(">") if part.strip()]
    lines = [f"% Issue {row.get('ID', '?')}: {row.get('Title', '').strip()}"]
    if row.get("Description"):
        lines.append(f"% Description: {row['Description'].strip()}")
    if row.get("Visualization"):
        lines.append(f"% Visualization: {row['Visualization'].strip()}")
    if row.get("Acceptance"):
        lines.append(f"% Acceptance: {row['Acceptance'].strip()}")
    lines.append("% Add 2-4 bullet-style notes or seed citations before drafting full prose.")
    lines.append("")
    for index, title in enumerate(path_parts, start=1):
        command = LEVEL_COMMAND.get(index)
        if command is None:
            break
        lines.append(f"\\{command}{{{title}}}")
    lines.append("% TODO: Draft this issue here.")
    lines.append("")
    return "\n".join(lines)


def find_insertion_anchor(content: str) -> int:
    for marker in ["\\label{ReferencesStart}", "\\bibliography{ref}", "\\end{document}"]:
        index = content.find(marker)
        if index != -1:
            return index
    return len(content)


def ancestor_paths(section_path: str) -> list[str]:
    parts = [part.strip() for part in section_path.split(">") if part.strip()]
    ancestors = []
    for index in range(1, len(parts)):
        ancestors.append(" > ".join(parts[:index]))
    return ancestors


def cmd_render_skeleton(args: argparse.Namespace) -> int:
    issues_path = resolve_issues_path(args.project_dir, args.issues)
    header, rows, schema = read_issue_table(issues_path)
    if "Section_Path" not in header:
        return fail("render-skeleton requires the current issues schema with Section_Path")

    issue = next((row for row in rows if row.get("ID") == args.issue_id), None)
    if issue is None:
        return fail(f"issue not found: {args.issue_id}")
    if issue.get("Phase") != "Writing":
        return fail(f"issue is not a Writing issue: {args.issue_id}")

    skeleton = render_issue_skeleton_text(issue)
    print(skeleton)
    if not args.apply_if_missing:
        return 0

    tex_path = args.project_dir / "main.tex"
    if not tex_path.exists():
        return fail(f"main.tex not found in {args.project_dir}")
    content = tex_path.read_text(encoding="utf-8")
    section_ranges = build_section_ranges(content)
    normalized_path = normalize_text(issue.get("Section_Path", ""))
    if normalized_path in section_ranges:
        print("Section already exists in main.tex; nothing to apply.")
        return 0

    existing_ancestors = [path for path in ancestor_paths(issue.get("Section_Path", "")) if normalize_text(path) in section_ranges]
    if existing_ancestors:
        return fail(
            "cannot auto-insert nested section when an ancestor already exists; "
            "copy the rendered skeleton manually into the parent section"
        )

    anchor = find_insertion_anchor(content)
    insertion = skeleton.rstrip() + "\n\n"
    updated = content[:anchor].rstrip() + "\n\n" + insertion + content[anchor:]
    tex_path.write_text(updated, encoding="utf-8")
    print(f"Inserted skeleton into: {tex_path}")
    return 0


def cmd_sync_verified(args: argparse.Namespace) -> int:
    issues_path = resolve_issues_path(args.project_dir, args.issues)
    analysis = build_issue_analysis(args.project_dir, issues_path)
    if "Section_Path" not in analysis["header"]:
        return fail("sync-verified requires an issues schema with Section_Path")

    changed_rows = 0
    changed_status = 0
    row_updates = {
        item["issue_id"]: item
        for item in analysis["writing_rows"]
    }
    for row in analysis["rows"]:
        if row.get("Phase") != "Writing":
            continue
        item = row_updates.get(row.get("ID", ""))
        if item is None:
            continue
        actual_verified = len(item["verified_keys"])
        if parse_int(row.get("Verified_Citations", "0")) != actual_verified:
            row["Verified_Citations"] = str(actual_verified)
            changed_rows += 1
        if args.set_done_if_complete:
            target = item["target_citations"]
            if item["section_exists"] and not item["placeholders"] and actual_verified >= target > 0:
                if row.get("Status") != "DONE":
                    row["Status"] = "DONE"
                    changed_status += 1

    if args.dry_run:
        print(f"Would update Verified_Citations in {changed_rows} row(s).")
        if args.set_done_if_complete:
            print(f"Would update Status to DONE in {changed_status} row(s).")
        return 0

    if changed_rows == 0 and changed_status == 0:
        print("No issues rows needed updates.")
        return 0

    write_issue_table(issues_path, analysis["header"], analysis["rows"])
    print(f"Updated issues file: {issues_path}")
    print(f"Verified_Citations updated in {changed_rows} row(s).")
    if args.set_done_if_complete:
        print(f"Status updated to DONE in {changed_status} row(s).")
    return 0


def cmd_append_bibtex(args: argparse.Namespace) -> int:
    bib_path = args.project_dir / "ref.bib"
    bib_path.parent.mkdir(parents=True, exist_ok=True)
    existing_content = bib_path.read_text(encoding="utf-8") if bib_path.exists() else ""
    existing_keys = {entry["key"] for entry in parse_bibtex_entries(existing_content)}

    appended = 0
    with connect(args.db) as conn:
        init_schema(conn)
        ensure_initialized(conn)
        for arxiv_id in args.arxiv_id:
            work_id = ensure_work(conn, arxiv_id=arxiv_id, timeout_s=args.timeout_s)
            if work_id is None:
                print(f"warning: could not fetch metadata for {arxiv_id}", file=sys.stderr)
                continue
            bibtex_text = ensure_bibtex(
                conn,
                arxiv_id=arxiv_id,
                work_id=work_id,
                timeout_s=args.timeout_s,
                refresh=args.refresh,
            )
            if bibtex_text is None:
                print(f"warning: empty BibTeX for {arxiv_id}", file=sys.stderr)
                continue
            cite_key = ensure_citation_key(conn, work_id=work_id)
            if cite_key in existing_keys:
                print(f"- skip key={cite_key} arxiv={arxiv_id} (already in ref.bib)")
                continue
            rewritten = rewrite_bibtex_key(bibtex_text, cite_key).rstrip() + "\n\n"
            with bib_path.open("a", encoding="utf-8") as handle:
                handle.write(rewritten)
            existing_keys.add(cite_key)
            appended += 1
            print(f"- appended key={cite_key} arxiv={arxiv_id}")

    print(f"Appended {appended} BibTeX entr{'y' if appended == 1 else 'ies'} to {bib_path}")
    if args.issue_id:
        print(f"Next: cite the new keys in main.tex for {args.issue_id}, then run sync-verified.")
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    issues_path = resolve_issues_path(args.project_dir, args.issues)
    analysis = build_issue_analysis(args.project_dir, issues_path)
    print_analysis_summary(issues_path, analysis)

    if args.fail_on_warning and (analysis["failures"] > 0 or analysis["warnings"] > 0):
        return 1
    if args.fail_on_issues and analysis["failures"] > 0:
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Issue execution helpers for the paper workflow.")
    parser.add_argument("--project-dir", required=True, help="Paper/project directory containing main.tex and ref.bib.")
    parser.add_argument("--db", help="Registry sqlite path. Defaults to <project-dir>/notes/arxiv-registry.sqlite3.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_audit = sub.add_parser("audit", help="Audit issues, sections, citations, and lightweight QA signals.")
    p_audit.add_argument("--issues", type=Path, help="Path to issues CSV. Defaults to latest file in <project-dir>/issues/.")
    p_audit.add_argument("--fail-on-issues", action="store_true", help="Return non-zero when FAIL findings exist.")
    p_audit.add_argument("--fail-on-warning", action="store_true", help="Return non-zero when WARN or FAIL findings exist.")
    p_audit.set_defaults(fn=cmd_audit)

    p_sync = sub.add_parser("sync-verified", help="Sync actual verified citation counts back into issues CSV.")
    p_sync.add_argument("--issues", type=Path, help="Path to issues CSV. Defaults to latest file in <project-dir>/issues/.")
    p_sync.add_argument("--dry-run", action="store_true", help="Report changes without writing the CSV.")
    p_sync.add_argument(
        "--set-done-if-complete",
        action="store_true",
        help="Also mark Writing issues DONE when section exists, has no placeholders, and verified citations meet target.",
    )
    p_sync.set_defaults(fn=cmd_sync_verified)

    p_bib = sub.add_parser("append-bibtex", help="Append stable-key BibTeX entries for arXiv IDs into ref.bib.")
    p_bib.add_argument("--issue-id", help="Optional issue ID for follow-up logging.")
    p_bib.add_argument("--timeout-s", type=int, default=20, help="Network timeout in seconds.")
    p_bib.add_argument("--refresh", action="store_true", help="Refresh remote BibTeX even if cached.")
    p_bib.add_argument("arxiv_id", nargs="+", help="One or more arXiv IDs to append.")
    p_bib.set_defaults(fn=cmd_append_bibtex)

    p_render = sub.add_parser("render-skeleton", help="Render or insert a LaTeX section skeleton for a Writing issue.")
    p_render.add_argument("--issues", type=Path, help="Path to issues CSV. Defaults to latest file in <project-dir>/issues/.")
    p_render.add_argument("--issue-id", required=True, help="Writing issue ID to scaffold.")
    p_render.add_argument(
        "--apply-if-missing",
        action="store_true",
        help="Insert the rendered skeleton into main.tex only when the full section path is entirely missing.",
    )
    p_render.set_defaults(fn=cmd_render_skeleton)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    args.project_dir = Path(args.project_dir).expanduser().resolve()
    args.db = Path(args.db).expanduser().resolve() if args.db else (args.project_dir / "notes" / "arxiv-registry.sqlite3").resolve()
    try:
        return int(args.fn(args))
    except FileNotFoundError as exc:
        return fail(str(exc))
    except ValueError as exc:
        return fail(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
