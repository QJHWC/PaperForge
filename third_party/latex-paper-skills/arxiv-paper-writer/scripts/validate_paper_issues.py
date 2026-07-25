#!/usr/bin/env python3
"""Validate paper issues CSV schema and required fields."""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

REQUIRED_COLUMNS = [
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
    "Depends_On",
    "Must_Verify",
    "Notes",
]

ALLOWED_STATUS = {"TODO", "DOING", "DONE", "SKIP"}
ALLOWED_PHASES = {"Research", "Writing", "Refinement", "QA"}
ALLOWED_SOURCE_POLICIES = {"core", "standard", "frontier"}
PHASE_PREFIX = {
    "Research": "R",
    "Writing": "W",
    "Refinement": "RF",
    "QA": "Q",
}


def fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


def looks_like_placeholder(value: str) -> bool:
    """Detect template-like placeholder values."""
    normalized = value.strip().lower()
    return not normalized or normalized in {"tbd", "todo"} or ("<" in value and ">" in value)


def parse_non_negative_int(raw: str, *, row_idx: int, column: str, errors: list[str]) -> int:
    """Parse a non-negative integer cell."""
    try:
        value = int(raw.strip())
    except ValueError:
        errors.append(f"row {row_idx}: '{column}' must be a non-negative integer")
        return 0
    if value < 0:
        errors.append(f"row {row_idx}: '{column}' must be non-negative")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate paper issues CSV schema and required fields."
    )
    parser.add_argument(
        "issues_csv",
        type=Path,
        help="Path to the issues CSV file to validate.",
    )
    args = parser.parse_args()

    path = args.issues_csv
    if not path.exists():
        return fail(f"file not found: {path}")

    rows = []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        for row in reader:
            if any(cell.strip() for cell in row):
                rows.append(row)

    if not rows:
        return fail("csv is empty")

    header = rows[0]
    if header != REQUIRED_COLUMNS:
        return fail(
            "invalid header. expected: "
            + ",".join(REQUIRED_COLUMNS)
            + " | got: "
            + ",".join(header)
        )

    seen_ids: set[str] = set()
    total_target_citations = 0
    total_verified_citations = 0
    status_counts = {key: 0 for key in ALLOWED_STATUS}
    phase_counts = {key: 0 for key in ALLOWED_PHASES}
    source_policy_counts = {key: 0 for key in ALLOWED_SOURCE_POLICIES}
    errors: list[str] = []

    for idx, row in enumerate(rows[1:], start=2):
        if len(row) != len(REQUIRED_COLUMNS):
            errors.append(f"row {idx}: expected {len(REQUIRED_COLUMNS)} columns, got {len(row)}")
            continue

        row_data = dict(zip(REQUIRED_COLUMNS, row))

        for col in ["ID", "Phase", "Title", "Description", "Acceptance", "Status", "Notes"]:
            if not row_data[col].strip():
                errors.append(f"row {idx}: '{col}' is empty")

        phase = row_data["Phase"].strip()
        if phase not in ALLOWED_PHASES:
            errors.append(f"row {idx}: 'Phase' must be one of {sorted(ALLOWED_PHASES)}, got '{phase}'")
        else:
            phase_counts[phase] += 1

        status = row_data["Status"].strip()
        if status not in ALLOWED_STATUS:
            errors.append(f"row {idx}: 'Status' must be one of {sorted(ALLOWED_STATUS)}, got '{status}'")
        else:
            status_counts[status] += 1

        issue_id = row_data["ID"].strip()
        if issue_id in seen_ids:
            errors.append(f"row {idx}: duplicate ID '{issue_id}'")
        seen_ids.add(issue_id)
        if phase in PHASE_PREFIX and not issue_id.startswith(PHASE_PREFIX[phase]):
            errors.append(f"row {idx}: ID '{issue_id}' does not match phase prefix '{PHASE_PREFIX[phase]}'")

        target = parse_non_negative_int(row_data["Target_Citations"], row_idx=idx, column="Target_Citations", errors=errors)
        verified = parse_non_negative_int(
            row_data["Verified_Citations"],
            row_idx=idx,
            column="Verified_Citations",
            errors=errors,
        )
        total_target_citations += target
        total_verified_citations += verified

        if looks_like_placeholder(row_data["Acceptance"]):
            errors.append(f"row {idx}: 'Acceptance' still contains a placeholder or TBD value")

        section_path = row_data["Section_Path"].strip()
        source_policy = row_data["Source_Policy"].strip().lower()
        if phase == "Writing":
            if not section_path or section_path.upper() == "N/A":
                errors.append(f"row {idx}: Writing issues must define 'Section_Path'")
            if source_policy not in ALLOWED_SOURCE_POLICIES:
                errors.append(
                    f"row {idx}: Writing issues must set 'Source_Policy' to one of {sorted(ALLOWED_SOURCE_POLICIES)}"
                )
            else:
                source_policy_counts[source_policy] += 1
            if looks_like_placeholder(row_data["Visualization"]):
                errors.append(f"row {idx}: Writing issues must define a concrete 'Visualization' plan")
            if target <= 0:
                errors.append(f"row {idx}: Writing issues must set 'Target_Citations' > 0")
            if status == "DONE" and verified <= 0:
                errors.append(f"row {idx}: DONE writing issues must record Verified_Citations > 0")
        else:
            if section_path and section_path.upper() != "N/A":
                errors.append(f"row {idx}: only Writing issues may set 'Section_Path' (use N/A otherwise)")
            if source_policy and source_policy.upper() != "N/A":
                errors.append(f"row {idx}: only Writing issues may set 'Source_Policy' (use N/A otherwise)")

        if status == "DONE" and verified < target:
            if phase == "Writing":
                errors.append(
                    f"row {idx}: DONE writing issue has Verified_Citations ({verified}) below Target_Citations ({target})"
                )
        if re.search(r"<[^>]+>", row_data["Title"] + row_data["Description"] + row_data["Notes"]):
            errors.append(f"row {idx}: unresolved angle-bracket placeholder remains in row content")

    # --- Dependency & Must_Verify consistency check ---
    parsed_rows: list[tuple[int, dict[str, str]]] = []
    for idx, row in enumerate(rows[1:], start=2):
        if len(row) == len(REQUIRED_COLUMNS):
            parsed_rows.append((idx, dict(zip(REQUIRED_COLUMNS, row))))

    done_or_skip = {
        data["ID"].strip()
        for _, data in parsed_rows
        if data["Status"].strip() in {"DONE", "SKIP"}
    }
    for idx, data in parsed_rows:
        if data["Status"].strip() == "DONE":
            deps_raw = data["Depends_On"].strip()
            deps = [
                d.strip()
                for d in deps_raw.split(";")
                if d.strip() and d.strip().upper() != "N/A"
            ]
            for dep in deps:
                if dep not in done_or_skip:
                    errors.append(
                        f"row {idx}: issue '{data['ID'].strip()}' is DONE "
                        f"but dependency '{dep}' is not DONE/SKIP"
                    )
            must_verify = data["Must_Verify"].strip().lower()
            if must_verify == "yes":
                verified = 0
                try:
                    verified = int(data["Verified_Citations"].strip())
                except ValueError:
                    pass
                if verified <= 0:
                    errors.append(
                        f"row {idx}: issue '{data['ID'].strip()}' is DONE with "
                        f"Must_Verify=yes but Verified_Citations is {verified}"
                    )

    if errors:
        for message in errors:
            print(f"error: {message}", file=sys.stderr)
        print(f"\nValidation failed with {len(errors)} error(s).", file=sys.stderr)
        return 1

    print("Validation passed!")
    print("\nSummary:")
    print(f"  Total issues: {len(rows) - 1}")
    print(
        "  By phase: "
        f"Research={phase_counts['Research']}, "
        f"Writing={phase_counts['Writing']}, "
        f"Refinement={phase_counts['Refinement']}, "
        f"QA={phase_counts['QA']}"
    )
    print(
        "  By status: "
        f"TODO={status_counts['TODO']}, DOING={status_counts['DOING']}, "
        f"DONE={status_counts['DONE']}, SKIP={status_counts['SKIP']}"
    )
    print(
        "  Writing source policy: "
        f"core={source_policy_counts['core']}, "
        f"standard={source_policy_counts['standard']}, "
        f"frontier={source_policy_counts['frontier']}"
    )
    print(f"  Target citations: {total_target_citations}")
    print(f"  Verified citations: {total_verified_citations}")

    if total_target_citations > 0:
        progress = (total_verified_citations / total_target_citations) * 100
        print(f"  Citation progress: {progress:.1f}%")

    if len(rows) > 1:
        completion = (status_counts["DONE"] / (len(rows) - 1)) * 100
        print(f"  Task completion: {completion:.1f}%")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
