#!/usr/bin/env python3
"""Recommend and audit citations using venue and source-quality policy."""

from __future__ import annotations

import argparse
import csv
import re
import sqlite3
import sys
import urllib.parse
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared"))

from arxiv_registry import connect, ensure_initialized, init_schema  # noqa: E402
from paper_utils import (  # noqa: E402
    count_citations,
    extract_bibtex_field,
    extract_citation_commands,
    extract_cite_context,
    extract_section_events,
    find_section_path_for_position,
    load_paper_config,
    normalize_text,
    normalize_text_tokens,
    now_iso,
    parse_bibtex_entries,
)
from source_policy_utils import (  # noqa: E402
    assess_work,
    build_crossref_candidate,
    ensure_policy_schema,
    json_fetch,
    load_external_metadata,
    resolve_work_ids,
    title_similarity,
    venue_matches,
)


def read_issue_rows(path: Path) -> list[dict[str, str]]:
    """Read issues CSV rows."""
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def build_document_tokens(issue_row: dict[str, str]) -> set[str]:
    """Build normalized keyword tokens from an issue row."""
    fields = [
        issue_row.get("Title", ""),
        issue_row.get("Section_Path", ""),
        issue_row.get("Description", ""),
        issue_row.get("Visualization", ""),
        issue_row.get("Acceptance", ""),
    ]
    tokens: set[str] = set()
    for field in fields:
        for token in normalize_text_tokens(field):
            if len(token) >= 3:
                tokens.add(token)
    return tokens


def work_candidate_rows(conn: sqlite3.Connection, work_ids: list[int]) -> list[sqlite3.Row]:
    """Load candidate rows with assessment metadata."""
    rows: list[sqlite3.Row] = []
    for work_id in work_ids:
        row = conn.execute(
            """
            SELECT w.work_id, w.arxiv_id, w.title, w.summary, w.published, w.journal_ref, w.doi,
                   sa.source_tier, sa.quality_score, sa.assessment_reason,
                   sa.canonical_venue, sa.preferred_citation_url, sa.has_formal_version
            FROM works w
            LEFT JOIN source_assessments sa ON sa.work_id = w.work_id
            WHERE w.work_id = ?;
            """,
            (work_id,),
        ).fetchone()
        if row is not None:
            rows.append(row)
    return rows


def recommendation_score(row: sqlite3.Row, issue_tokens: set[str], preferred_venues: list[str]) -> float:
    """Score a candidate source for an issue."""
    haystack = " ".join(
        [
            str(row["title"] or ""),
            str(row["summary"] or ""),
            str(row["canonical_venue"] or row["journal_ref"] or ""),
        ]
    )
    haystack_tokens = set(token for token in normalize_text_tokens(haystack) if len(token) >= 3)
    overlap = len(issue_tokens & haystack_tokens)
    score = overlap * 8.0
    quality_score = int(row["quality_score"] or 0)
    score += quality_score
    if venue_matches(str(row["canonical_venue"] or row["journal_ref"] or ""), preferred_venues):
        score += 10.0
    if row["source_tier"] == "A":
        score += 5.0
    elif row["source_tier"] == "B":
        score += 2.0
    return score


def cmd_recommend(args: argparse.Namespace) -> int:
    issues = read_issue_rows(args.issues)
    issue_row = next((row for row in issues if row.get("ID") == args.issue_id), None)
    if issue_row is None:
        print(f"error: issue not found: {args.issue_id}", file=sys.stderr)
        return 1

    config = load_paper_config(args.project_dir)
    preferred_venues = [str(item) for item in (config.get("preferred_venues") or [])]
    target_venue = str(config.get("target_venue") or "")
    if target_venue and target_venue not in preferred_venues:
        preferred_venues.insert(0, target_venue)

    with connect(args.db) as conn:
        init_schema(conn)
        ensure_initialized(conn)
        ensure_policy_schema(conn)
        work_ids = resolve_work_ids(conn, search_id=args.search_id, arxiv_ids=args.arxiv_id)
        issue_tokens = build_document_tokens(issue_row)

        ranked: list[tuple[float, sqlite3.Row]] = []
        for row in work_candidate_rows(conn, work_ids):
            if row["quality_score"] is None:
                assessment = assess_work(
                    conn,
                    work_id=int(row["work_id"]),
                    config=config,
                    timeout_s=args.timeout_s,
                    refresh=args.refresh,
                )
                row = conn.execute(
                    """
                    SELECT w.work_id, w.arxiv_id, w.title, w.summary, w.published, w.journal_ref, w.doi,
                           sa.source_tier, sa.quality_score, sa.assessment_reason,
                           sa.canonical_venue, sa.preferred_citation_url, sa.has_formal_version
                    FROM works w
                    LEFT JOIN source_assessments sa ON sa.work_id = w.work_id
                    WHERE w.work_id = ?;
                    """,
                    (assessment["work_id"],),
                ).fetchone()
            if row is None:
                continue
            score = recommendation_score(row, issue_tokens, preferred_venues)
            ranked.append((score, row))

        ranked.sort(key=lambda item: item[0], reverse=True)
        print(
            f"Recommendations for {args.issue_id} ({issue_row.get('Section_Path') or 'N/A'}) | "
            f"source_policy={issue_row.get('Source_Policy') or 'N/A'}"
        )
        for score, row in ranked[: args.limit]:
            print(
                f"- score={score:.1f} tier={row['source_tier'] or 'N/A'} venue={row['canonical_venue'] or row['journal_ref'] or 'N/A'} "
                f"title={row['title']} url={row['preferred_citation_url'] or 'N/A'}"
            )
    return 0


def cmd_audit_bib(args: argparse.Namespace) -> int:
    bib_path = args.project_dir / "ref.bib"
    if not bib_path.exists():
        print(f"error: ref.bib not found in {args.project_dir}", file=sys.stderr)
        return 1

    content = bib_path.read_text(encoding="utf-8")
    entries = parse_bibtex_entries(content)
    if not entries:
        print("No BibTeX entries found.")
        return 0

    flagged = 0
    with connect(args.db) as conn:
        init_schema(conn)
        ensure_initialized(conn)
        ensure_policy_schema(conn)
        for entry in entries:
            key_row = conn.execute(
                "SELECT work_id FROM citation_keys WHERE key = ?;",
                (entry["key"],),
            ).fetchone()
            if key_row is None:
                continue
            work_id = int(key_row["work_id"])
            assessment = conn.execute(
                "SELECT * FROM source_assessments WHERE work_id = ?;",
                (work_id,),
            ).fetchone()
            metadata = load_external_metadata(conn, work_id)
            if assessment is None or metadata is None:
                continue
            uses_arxiv_bib = bool(re.search(r"\b(eprint|archivePrefix)\b|arxiv\.org", entry["text"], re.IGNORECASE))
            if uses_arxiv_bib and int(assessment["has_formal_version"] or 0):
                flagged += 1
                print(
                    f"- key={entry['key']} tier={assessment['source_tier']} venue={assessment['canonical_venue'] or 'N/A'} "
                    f"doi={metadata.get('canonical_doi') or 'N/A'} url={metadata.get('canonical_url') or 'N/A'}"
                )

    if flagged == 0:
        print("No BibTeX entries require formal-version replacement.")
        return 0
    print(f"Flagged {flagged} BibTeX entr{'y' if flagged == 1 else 'ies'} that should prefer a formal version.")
    return 1 if args.fail_on_policy else 0


def normalize_section_lookup_map(content: str) -> dict[str, set[str]]:
    """Map normalized section paths to cited keys (including descendants)."""
    sections = extract_section_events(content)
    citations = extract_citation_commands(content)
    expanded: dict[str, set[str]] = {}
    for citation in citations:
        path = find_section_path_for_position(sections, int(citation["start"]))
        if not path:
            expanded.setdefault("", set()).update(citation["keys"])
            continue
        path_parts = [part.strip() for part in path.split(">") if part.strip()]
        for index in range(1, len(path_parts) + 1):
            ancestor = " > ".join(path_parts[:index])
            expanded.setdefault(normalize_text(ancestor), set()).update(citation["keys"])
    return expanded


def policy_threshold(policy: str, config: dict[str, Any]) -> tuple[float, float | None]:
    """Return the minimum A/B ratio and max C ratio for a source policy."""
    source_policy = config.get("source_policy") or {}
    if policy == "core":
        return float(source_policy.get("min_tier_ab_ratio_core", 0.8)), float(source_policy.get("max_tier_c_ratio_core", 0.2))
    if policy == "standard":
        return float(source_policy.get("min_tier_ab_ratio_standard", 0.6)), None
    return 0.0, None


def cmd_audit_tex(args: argparse.Namespace) -> int:
    tex_path = args.project_dir / "main.tex"
    if not tex_path.exists():
        print(f"error: main.tex not found in {args.project_dir}", file=sys.stderr)
        return 1

    config = load_paper_config(args.project_dir)
    issues = read_issue_rows(args.issues)
    citation_map = normalize_section_lookup_map(tex_path.read_text(encoding="utf-8"))

    failures = 0
    with connect(args.db) as conn:
        init_schema(conn)
        ensure_initialized(conn)
        ensure_policy_schema(conn)
        for issue in issues:
            if issue.get("Phase") != "Writing":
                continue
            section_path = normalize_text(issue.get("Section_Path") or "")
            source_policy = (issue.get("Source_Policy") or "").strip().lower()
            cited_keys = citation_map.get(section_path, set())
            if not cited_keys:
                print(f"- {issue['ID']}: no citations found for section path '{issue.get('Section_Path')}'")
                # Only treat this as a policy failure if the writing issue expected citations.
                # (e.g., Abstract/Conclusion often legitimately have 0 citations.)
                target_raw = str(issue.get("Target_Citations") or "0").strip()
                target_citations = int(target_raw) if target_raw.isdigit() else 0
                if args.fail_on_policy and target_citations > 0:
                    failures += 1
                continue

            tier_counts = {"A": 0, "B": 0, "C": 0, "unknown": 0}
            for key in sorted(cited_keys):
                row = conn.execute(
                    """
                    SELECT sa.source_tier
                    FROM citation_keys ck
                    LEFT JOIN source_assessments sa ON sa.work_id = ck.work_id
                    WHERE ck.key = ?;
                    """,
                    (key,),
                ).fetchone()
                if row is None or row["source_tier"] is None:
                    tier_counts["unknown"] += 1
                else:
                    tier_counts[str(row["source_tier"])] += 1

            total_known = tier_counts["A"] + tier_counts["B"] + tier_counts["C"]
            ab_ratio = ((tier_counts["A"] + tier_counts["B"]) / total_known) if total_known else 0.0
            c_ratio = (tier_counts["C"] / total_known) if total_known else 0.0
            min_ab_ratio, max_c_ratio = policy_threshold(source_policy, config)

            status = "PASS"
            if source_policy in {"core", "standard"} and ab_ratio < min_ab_ratio:
                status = "FAIL"
            if source_policy == "core" and max_c_ratio is not None and c_ratio > max_c_ratio:
                status = "FAIL"
            if source_policy == "frontier" and tier_counts["C"] > 0:
                status = "WARN"
            if status == "FAIL":
                failures += 1

            print(
                f"- {issue['ID']} [{status}] section={issue.get('Section_Path')} policy={source_policy} "
                f"A={tier_counts['A']} B={tier_counts['B']} C={tier_counts['C']} unknown={tier_counts['unknown']} "
                f"AB_ratio={ab_ratio:.2f} C_ratio={c_ratio:.2f}"
            )

    return 1 if failures and args.fail_on_policy else 0


def cmd_lint_bib(args: argparse.Namespace) -> int:
    """Lint ref.bib against main.tex for orphans, danglers, dupes, missing fields."""
    bib_path = args.project_dir / "ref.bib"
    tex_path = args.project_dir / "main.tex"
    if not bib_path.exists():
        print(f"error: ref.bib not found in {args.project_dir}", file=sys.stderr)
        return 1
    if not tex_path.exists():
        print(f"error: main.tex not found in {args.project_dir}", file=sys.stderr)
        return 1

    bib_content = bib_path.read_text(encoding="utf-8")
    entries = parse_bibtex_entries(bib_content)
    citation_info = count_citations(tex_path)
    cited_keys = set(citation_info["keys"])

    bib_keys: list[str] = []
    seen_keys: dict[str, int] = {}
    duplicates: list[str] = []
    for entry in entries:
        key = entry["key"]
        bib_keys.append(key)
        if key in seen_keys:
            duplicates.append(key)
        seen_keys[key] = seen_keys.get(key, 0) + 1

    bib_key_set = set(bib_keys)
    orphans = sorted(bib_key_set - cited_keys)
    danglers = sorted(cited_keys - bib_key_set)

    missing_title: list[str] = []
    missing_author: list[str] = []
    missing_year: list[str] = []
    article_like = {"article", "inproceedings", "conference", "incollection"}
    for entry in entries:
        if not re.search(r"title\s*=", entry["text"], re.IGNORECASE):
            missing_title.append(entry["key"])
        if entry["entry_type"].lower() in article_like:
            if not re.search(r"author\s*=", entry["text"], re.IGNORECASE):
                missing_author.append(entry["key"])
        if not re.search(r"year\s*=", entry["text"], re.IGNORECASE):
            missing_year.append(entry["key"])

    fails = 0
    warns = 0
    print(f"BibTeX lint: ref.bib ({len(entries)} entries) vs main.tex ({citation_info['unique']} unique citations)")
    if danglers:
        fails += len(danglers)
        print(f"- FAIL: {len(danglers)} dangling citation(s) (cited but not in bib): {', '.join(danglers)}")
    if orphans:
        warns += len(orphans)
        print(f"- WARN: {len(orphans)} orphan entry/entries (in bib but never cited): {', '.join(orphans)}")
    if duplicates:
        fails += len(duplicates)
        print(f"- FAIL: {len(duplicates)} duplicate key(s): {', '.join(sorted(set(duplicates)))}")
    if missing_title:
        warns += len(missing_title)
        print(f"- WARN: {len(missing_title)} entry/entries missing 'title' field: {', '.join(missing_title)}")
    if missing_author:
        warns += len(missing_author)
        print(f"- WARN: {len(missing_author)} entry/entries missing 'author' field: {', '.join(missing_author)}")
    if missing_year:
        warns += len(missing_year)
        print(f"- WARN: {len(missing_year)} entry/entries missing 'year' field: {', '.join(missing_year)}")
    print(f"Summary: {fails} FAIL, {warns} WARN")
    if fails > 0 and args.fail_on_lint:
        return 1
    return 0


def _ensure_bib_verifications(conn: sqlite3.Connection) -> None:
    """Create the bib_verifications cache table if missing."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bib_verifications (
          bib_key TEXT PRIMARY KEY,
          title TEXT,
          author TEXT,
          year TEXT,
          crossref_doi TEXT,
          crossref_title TEXT,
          crossref_venue TEXT,
          match_score REAL NOT NULL DEFAULT 0,
          status TEXT NOT NULL,
          verified_at TEXT NOT NULL
        );
        """
    )


def _store_bib_verification(
    conn: sqlite3.Connection,
    key: str,
    title: str | None,
    author: str,
    year: str | None,
    cr_doi: str,
    cr_title: str,
    cr_venue: str,
    score: float,
    status: str,
) -> None:
    conn.execute(
        """
        INSERT INTO bib_verifications(bib_key, title, author, year,
          crossref_doi, crossref_title, crossref_venue,
          match_score, status, verified_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(bib_key) DO UPDATE SET
          title=excluded.title, author=excluded.author, year=excluded.year,
          crossref_doi=excluded.crossref_doi, crossref_title=excluded.crossref_title,
          crossref_venue=excluded.crossref_venue,
          match_score=excluded.match_score, status=excluded.status,
          verified_at=excluded.verified_at;
        """,
        (key, title, author, year, cr_doi, cr_title, cr_venue, score, status, now_iso()),
    )
    conn.commit()


def cmd_verify_bib(args: argparse.Namespace) -> int:
    """Verify all BibTeX entries against CrossRef by title+author search."""
    bib_path = args.project_dir / "ref.bib"
    if not bib_path.exists():
        print(f"error: ref.bib not found in {args.project_dir}", file=sys.stderr)
        return 1

    content = bib_path.read_text(encoding="utf-8")
    entries = parse_bibtex_entries(content)
    if not entries:
        print("No BibTeX entries found.")
        return 0

    with connect(args.db) as conn:
        init_schema(conn)
        ensure_initialized(conn)
        ensure_policy_schema(conn)
        _ensure_bib_verifications(conn)

        results: list[dict[str, Any]] = []
        for entry in entries:
            key = entry["key"]

            # Skip entries already tracked in the arXiv registry
            tracked = conn.execute(
                "SELECT work_id FROM citation_keys WHERE key = ?;", (key,)
            ).fetchone()
            if tracked is not None:
                results.append({"key": key, "status": "TRACKED", "score": 100.0, "title": ""})
                continue

            # Use cache unless --refresh
            if not args.refresh:
                cached = conn.execute(
                    "SELECT * FROM bib_verifications WHERE bib_key = ?;", (key,)
                ).fetchone()
                if cached is not None:
                    results.append({
                        "key": key,
                        "status": str(cached["status"]),
                        "score": float(cached["match_score"]),
                        "title": str(cached["crossref_title"] or ""),
                    })
                    continue

            title = extract_bibtex_field(entry["text"], "title")
            author = extract_bibtex_field(entry["text"], "author")
            year = extract_bibtex_field(entry["text"], "year")

            if not title and not author:
                _store_bib_verification(conn, key, title, "", year, "", "", "", 0.0, "MISSING_FIELDS")
                results.append({"key": key, "status": "MISSING_FIELDS", "score": 0.0, "title": ""})
                continue

            # Build CrossRef query
            query: dict[str, str] = {"rows": "3"}
            if title:
                query["query.title"] = title
            first_author = ""
            if author:
                first_author = author.split(" and ")[0].strip()
                if first_author:
                    query["query.author"] = first_author

            url = f"https://api.crossref.org/works?{urllib.parse.urlencode(query)}"
            _, payload = json_fetch(conn, kind="crossref_bib_verify", url=url, timeout_s=args.timeout_s)

            best_score = 0.0
            best_candidate: dict[str, Any] | None = None
            if isinstance(payload, dict):
                items = (((payload.get("message") or {}).get("items")) or [])[:3]
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    candidate = build_crossref_candidate(item)
                    score = title_similarity(title or "", str(candidate.get("title") or "")) * 70.0
                    if first_author:
                        norm_first = normalize_text(first_author)
                        candidate_authors = [normalize_text(a) for a in candidate.get("authors") or []]
                        if any(norm_first in a or a in norm_first for a in candidate_authors):
                            score += 15.0
                    if year and candidate.get("published_year"):
                        try:
                            if abs(int(year) - int(candidate["published_year"])) <= 1:
                                score += 10.0
                        except (ValueError, TypeError):
                            pass
                    if candidate.get("is_formal_publication"):
                        score += 5.0
                    if score > best_score:
                        best_score = score
                        best_candidate = candidate

            if best_score >= 70:
                status = "VERIFIED"
            elif best_score >= 45:
                status = "LIKELY"
            else:
                status = "UNVERIFIED"

            cr_doi = str((best_candidate or {}).get("doi") or "")
            cr_title = str((best_candidate or {}).get("title") or "")
            cr_venue = str((best_candidate or {}).get("venue") or "")

            _store_bib_verification(conn, key, title, first_author, year, cr_doi, cr_title, cr_venue, best_score, status)
            results.append({"key": key, "status": status, "score": best_score, "title": cr_title})

        # Summary
        print(f"BibTeX verification: {len(entries)} entries")
        status_counts: dict[str, int] = {}
        for r in results:
            status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1
        for st in sorted(status_counts):
            print(f"  {st}: {status_counts[st]}")

        unverified = [r for r in results if r["status"] in {"UNVERIFIED", "MISSING_FIELDS"}]
        if unverified:
            print("\nEntries needing attention:")
            for r in unverified:
                print(f"  - {r['key']}: {r['status']} (score={r['score']:.1f})")

        if args.fail_on_unverified and any(r["status"] == "UNVERIFIED" for r in results):
            return 1
    return 0


def cmd_audit_context(args: argparse.Namespace) -> int:
    """Audit citation-context relevance in main.tex."""
    tex_path = args.project_dir / "main.tex"
    if not tex_path.exists():
        print(f"error: main.tex not found in {args.project_dir}", file=sys.stderr)
        return 1

    content = tex_path.read_text(encoding="utf-8")
    citations = extract_citation_commands(content)
    sections = extract_section_events(content)

    if not citations:
        print("No citations found in main.tex.")
        return 0

    with connect(args.db) as conn:
        init_schema(conn)
        ensure_initialized(conn)
        ensure_policy_schema(conn)
        _ensure_bib_verifications(conn)

        results: list[dict[str, Any]] = []
        for cite_cmd in citations:
            section_path = find_section_path_for_position(sections, int(cite_cmd["start"]))
            context_text = extract_cite_context(content, int(cite_cmd["start"]), int(cite_cmd["end"]))

            for key in cite_cmd["keys"]:
                paper_title = ""
                paper_abstract = ""

                # Try the arXiv registry first
                row = conn.execute(
                    """
                    SELECT w.title, w.summary
                    FROM citation_keys ck
                    JOIN works w ON w.work_id = ck.work_id
                    WHERE ck.key = ?;
                    """,
                    (key,),
                ).fetchone()
                if row:
                    paper_title = str(row["title"] or "")
                    paper_abstract = str(row["summary"] or "")
                else:
                    # Fallback to bib_verifications
                    bv = conn.execute(
                        "SELECT crossref_title FROM bib_verifications WHERE bib_key = ?;",
                        (key,),
                    ).fetchone()
                    if bv and bv["crossref_title"]:
                        paper_title = str(bv["crossref_title"])

                if not paper_title:
                    results.append({
                        "key": key,
                        "section": section_path or "(preamble)",
                        "context": context_text[:120],
                        "paper_title": "(unknown)",
                        "score": -1.0,
                        "classification": "UNKNOWN",
                    })
                    continue

                reference_text = paper_title
                if paper_abstract:
                    reference_text += " " + paper_abstract
                score = title_similarity(context_text, reference_text)

                if score >= 0.15:
                    classification = "STRONG"
                elif score >= 0.05:
                    classification = "WEAK"
                else:
                    classification = "SUSPECT"

                results.append({
                    "key": key,
                    "section": section_path or "(preamble)",
                    "context": context_text[:120],
                    "paper_title": paper_title[:80],
                    "score": score,
                    "classification": classification,
                })

    # Report
    print(f"Citation-context audit: {len(results)} citation instances")
    cls_counts: dict[str, int] = {}
    for r in results:
        cls_counts[r["classification"]] = cls_counts.get(r["classification"], 0) + 1
    for cls in sorted(cls_counts):
        print(f"  {cls}: {cls_counts[cls]}")

    suspects = [r for r in results if r["classification"] == "SUSPECT"]
    weak = [r for r in results if r["classification"] == "WEAK"]

    if suspects:
        print("\nSUSPECT citations (score < 0.05):")
        for r in suspects:
            print(f"  - [{r['key']}] section={r['section']} score={r['score']:.3f}")
            print(f"    paper: {r['paper_title']}")
            print(f"    context: {r['context']}...")

    if weak and args.verbose:
        print("\nWEAK citations (0.05 <= score < 0.15):")
        for r in weak:
            print(f"  - [{r['key']}] section={r['section']} score={r['score']:.3f}")
            print(f"    paper: {r['paper_title']}")

    if args.fail_on_suspect and suspects:
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Recommend and audit citations using source policy.")
    parser.add_argument("--project-dir", required=True, help="Paper/project directory.")
    parser.add_argument("--db", help="Registry sqlite path. Defaults to <project-dir>/notes/arxiv-registry.sqlite3.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_recommend = sub.add_parser("recommend", help="Recommend citations for a single issue.")
    p_recommend.add_argument("--issues", required=True, type=Path, help="Path to issues CSV.")
    p_recommend.add_argument("--issue-id", required=True, help="Issue ID to recommend sources for.")
    p_recommend.add_argument("--search-id", type=int, help="Limit recommendations to a stored search result set.")
    p_recommend.add_argument("--arxiv-id", nargs="*", default=[], help="Optional extra arXiv IDs to include.")
    p_recommend.add_argument("--limit", type=int, default=10, help="Number of recommendations to print.")
    p_recommend.add_argument("--timeout-s", type=int, default=20, help="Network timeout in seconds.")
    p_recommend.add_argument("--refresh", action="store_true", help="Refresh source assessments before ranking.")
    p_recommend.set_defaults(fn=cmd_recommend)

    p_bib = sub.add_parser("audit-bib", help="Audit ref.bib for arXiv entries that should prefer formal versions.")
    p_bib.add_argument("--fail-on-policy", action="store_true", help="Return a non-zero exit code when policy violations are found.")
    p_bib.set_defaults(fn=cmd_audit_bib)

    p_tex = sub.add_parser("audit-tex", help="Audit main.tex citations against issues source policy.")
    p_tex.add_argument("--issues", required=True, type=Path, help="Path to issues CSV.")
    p_tex.add_argument("--fail-on-policy", action="store_true", help="Return a non-zero exit code when policy violations are found.")
    p_tex.set_defaults(fn=cmd_audit_tex)

    p_lint = sub.add_parser("lint-bib", help="Lint ref.bib for orphans, danglers, duplicates, and missing fields.")
    p_lint.add_argument("--fail-on-lint", action="store_true", help="Return non-zero exit code when lint issues are found.")
    p_lint.set_defaults(fn=cmd_lint_bib)

    p_verify = sub.add_parser("verify-bib", help="Verify non-arXiv BibTeX entries against CrossRef.")
    p_verify.add_argument("--fail-on-unverified", action="store_true", help="Return non-zero if any entries are UNVERIFIED.")
    p_verify.add_argument("--timeout-s", type=int, default=20, help="Network timeout in seconds.")
    p_verify.add_argument("--refresh", action="store_true", help="Re-verify even if cached.")
    p_verify.set_defaults(fn=cmd_verify_bib)

    p_ctx = sub.add_parser("audit-context", help="Audit citation-context relevance in main.tex.")
    p_ctx.add_argument("--fail-on-suspect", action="store_true", help="Return non-zero if SUSPECT citations exist.")
    p_ctx.add_argument("--verbose", action="store_true", help="Also print WEAK citation details.")
    p_ctx.set_defaults(fn=cmd_audit_context)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    args.project_dir = Path(args.project_dir).expanduser().resolve()
    args.db = Path(args.db).expanduser().resolve() if args.db else (args.project_dir / "notes" / "arxiv-registry.sqlite3").resolve()
    return int(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
