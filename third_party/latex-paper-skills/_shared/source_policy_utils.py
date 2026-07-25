#!/usr/bin/env python3
"""Shared helpers for source ranking and citation policy."""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import urllib.parse
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from arxiv_registry import (  # noqa: E402
    ensure_initialized,
    ensure_work,
    fetch_url,
    init_schema,
    normalize_arxiv_id,
    record_fetch,
)
from paper_utils import load_paper_config, normalize_text, normalize_text_tokens, now_iso

FORMAL_SOURCE_TYPES = {
    "journal-article",
    "proceedings-article",
    "book-chapter",
    "book",
    "journal",
    "conference",
}


def ensure_policy_schema(conn: sqlite3.Connection) -> None:
    """Create metadata and assessment tables if missing."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS external_metadata (
          work_id INTEGER PRIMARY KEY REFERENCES works(work_id) ON DELETE CASCADE,
          provider TEXT,
          canonical_title TEXT,
          canonical_doi TEXT,
          canonical_url TEXT,
          canonical_source_type TEXT,
          canonical_venue TEXT,
          publisher TEXT,
          published_year INTEGER,
          is_formal_publication INTEGER NOT NULL DEFAULT 0,
          match_score REAL NOT NULL DEFAULT 0,
          matched_by TEXT,
          crossref_json TEXT,
          openalex_json TEXT,
          last_enriched_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS source_assessments (
          work_id INTEGER PRIMARY KEY REFERENCES works(work_id) ON DELETE CASCADE,
          source_tier TEXT NOT NULL,
          quality_score INTEGER NOT NULL,
          assessment_reason TEXT NOT NULL,
          has_formal_version INTEGER NOT NULL DEFAULT 0,
          canonical_source_type TEXT,
          canonical_venue TEXT,
          preferred_citation_url TEXT,
          assessed_at TEXT NOT NULL
        );
        """
    )


def normalize_doi(value: str | None) -> str | None:
    """Normalize DOI values and DOI URLs."""
    if not value:
        return None
    doi = value.strip()
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi, flags=re.IGNORECASE)
    doi = re.sub(r"^doi:\s*", "", doi, flags=re.IGNORECASE)
    doi = doi.strip()
    return doi or None


def normalize_venue_name(value: str | None) -> str:
    """Normalize venue names for comparisons."""
    return re.sub(r"\s+", " ", (value or "").strip()).lower()


def venue_matches(venue: str | None, preferred_venues: list[str]) -> bool:
    """Return True when a venue loosely matches a preferred venue."""
    if not venue:
        return False
    normalized = normalize_venue_name(venue)
    for candidate in preferred_venues:
        preferred = normalize_venue_name(candidate)
        if preferred and (preferred in normalized or normalized in preferred):
            return True
    return False


def parse_year(value: Any) -> int | None:
    """Extract a year from a date-like string."""
    if value is None:
        return None
    match = re.search(r"(19|20)\d{2}", str(value))
    if match is None:
        return None
    return int(match.group(0))


def json_fetch(conn: sqlite3.Connection, *, kind: str, url: str, timeout_s: int) -> tuple[int | None, Any]:
    """Fetch and decode JSON while recording the request."""
    status, body = fetch_url(url, timeout_s=timeout_s)
    if body:
        record_fetch(conn, kind=kind, url=url, status=status, body=body)
    if not body:
        return status, None
    try:
        return status, json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return status, None


def load_work_row(conn: sqlite3.Connection, work_id: int) -> sqlite3.Row:
    """Load a work row with the first author."""
    row = conn.execute(
        """
        SELECT w.work_id, w.arxiv_id, w.title, w.summary, w.published, w.updated, w.comment,
               w.primary_category, w.categories_json, w.abs_url, w.pdf_url, w.journal_ref, w.doi,
               (
                   SELECT a.name
                   FROM work_authors wa
                   JOIN authors a ON a.author_id = wa.author_id
                   WHERE wa.work_id = w.work_id
                   ORDER BY wa.position ASC
                   LIMIT 1
               ) AS first_author
        FROM works w
        WHERE w.work_id = ?;
        """,
        (work_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"work not found: work_id={work_id}")
    return row


def title_similarity(left: str, right: str) -> float:
    """Compute a simple token-overlap title similarity."""
    left_tokens = set(normalize_text_tokens(left))
    right_tokens = set(normalize_text_tokens(right))
    if not left_tokens or not right_tokens:
        return 0.0
    overlap = left_tokens & right_tokens
    union = left_tokens | right_tokens
    return len(overlap) / max(len(union), 1)


def build_crossref_candidate(item: dict[str, Any]) -> dict[str, Any]:
    """Convert a Crossref work record to a normalized candidate."""
    title_values = item.get("title") or []
    title = title_values[0] if title_values else ""
    container = item.get("container-title") or []
    venue = container[0] if container else item.get("publisher") or ""
    year = None
    for key in ["published-print", "published-online", "issued"]:
        date_parts = (((item.get(key) or {}).get("date-parts") or [])[:1] or [[]])[0]
        if not date_parts:
            continue
        candidate_year = parse_year(date_parts[0])
        if candidate_year is not None:
            year = candidate_year
            break
    authors = []
    for author in item.get("author") or []:
        family = (author.get("family") or "").strip()
        given = (author.get("given") or "").strip()
        display = ", ".join(part for part in [family, given] if part) or author.get("name") or ""
        if display:
            authors.append(display)
    source_type = str(item.get("type") or "")
    return {
        "provider": "crossref",
        "title": title,
        "doi": normalize_doi(item.get("DOI")),
        "url": item.get("URL"),
        "venue": venue,
        "publisher": item.get("publisher"),
        "source_type": source_type,
        "published_year": year,
        "is_formal_publication": source_type in FORMAL_SOURCE_TYPES,
        "authors": authors,
        "raw": item,
    }


def build_openalex_candidate(item: dict[str, Any]) -> dict[str, Any]:
    """Convert an OpenAlex work record to a normalized candidate."""
    location = item.get("primary_location") or {}
    source = location.get("source") or {}
    ids = item.get("ids") or {}
    source_type = str(source.get("type") or item.get("type") or "")
    authors = []
    for authorship in item.get("authorships") or []:
        author = authorship.get("author") or {}
        display = (author.get("display_name") or "").strip()
        if display:
            authors.append(display)
    return {
        "provider": "openalex",
        "title": item.get("title") or "",
        "doi": normalize_doi(item.get("doi") or ids.get("doi")),
        "url": location.get("landing_page_url") or item.get("id"),
        "venue": source.get("display_name") or "",
        "publisher": source.get("host_organization_name") or "",
        "source_type": source_type,
        "published_year": item.get("publication_year"),
        "is_formal_publication": source_type in FORMAL_SOURCE_TYPES,
        "authors": authors,
        "raw": item,
    }


def candidate_match_score(work: sqlite3.Row, candidate: dict[str, Any]) -> float:
    """Score how well an external metadata candidate matches an arXiv work."""
    work_doi = normalize_doi(work["doi"])
    candidate_doi = normalize_doi(candidate.get("doi"))
    if work_doi and candidate_doi and work_doi == candidate_doi:
        return 100.0

    score = title_similarity(str(work["title"]), str(candidate.get("title") or "")) * 70.0
    work_year = parse_year(work["published"])
    candidate_year = parse_year(candidate.get("published_year"))
    if work_year is not None and candidate_year is not None and abs(work_year - candidate_year) <= 1:
        score += 10.0

    first_author = normalize_text(str(work["first_author"] or ""))
    candidate_authors = [normalize_text(author) for author in candidate.get("authors") or []]
    if first_author and any(first_author in author or author in first_author for author in candidate_authors):
        score += 15.0

    if candidate.get("is_formal_publication"):
        score += 5.0
    return score


def choose_best_candidate(work: sqlite3.Row, candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Choose the best external metadata candidate for a work."""
    ranked: list[tuple[float, dict[str, Any]]] = []
    for candidate in candidates:
        score = candidate_match_score(work, candidate)
        ranked.append((score, candidate))
    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0], reverse=True)
    score, candidate = ranked[0]
    if score < 45.0:
        return None
    candidate = dict(candidate)
    candidate["match_score"] = score
    return candidate


def fetch_crossref_metadata(conn: sqlite3.Connection, work: sqlite3.Row, timeout_s: int) -> dict[str, Any] | None:
    """Fetch Crossref metadata by DOI or title search."""
    work_doi = normalize_doi(work["doi"])
    if work_doi:
        url = f"https://api.crossref.org/works/{urllib.parse.quote(work_doi, safe='')}"
        _, payload = json_fetch(conn, kind="crossref_doi", url=url, timeout_s=timeout_s)
        if isinstance(payload, dict) and isinstance(payload.get("message"), dict):
            candidate = build_crossref_candidate(payload["message"])
            candidate["match_score"] = 100.0
            candidate["matched_by"] = "doi"
            return candidate

    query = {
        "query.title": str(work["title"]),
        "rows": "5",
    }
    if work["first_author"]:
        query["query.author"] = str(work["first_author"])
    url = f"https://api.crossref.org/works?{urllib.parse.urlencode(query)}"
    _, payload = json_fetch(conn, kind="crossref_search", url=url, timeout_s=timeout_s)
    if not isinstance(payload, dict):
        return None
    items = (((payload.get("message") or {}).get("items")) or [])[:5]
    candidates = [build_crossref_candidate(item) for item in items if isinstance(item, dict)]
    candidate = choose_best_candidate(work, candidates)
    if candidate is not None:
        candidate["matched_by"] = "title_search"
    return candidate


def fetch_openalex_metadata(conn: sqlite3.Connection, work: sqlite3.Row, timeout_s: int) -> dict[str, Any] | None:
    """Fetch OpenAlex metadata by DOI or title search."""
    work_doi = normalize_doi(work["doi"])
    if work_doi:
        filter_value = f"doi:https://doi.org/{work_doi}"
        url = f"https://api.openalex.org/works?{urllib.parse.urlencode({'filter': filter_value, 'per-page': 1})}"
        _, payload = json_fetch(conn, kind="openalex_doi", url=url, timeout_s=timeout_s)
        results = (payload or {}).get("results") if isinstance(payload, dict) else None
        if isinstance(results, list) and results:
            candidate = build_openalex_candidate(results[0])
            candidate["match_score"] = 100.0
            candidate["matched_by"] = "doi"
            return candidate

    query = {"search": str(work["title"]), "per-page": "5"}
    url = f"https://api.openalex.org/works?{urllib.parse.urlencode(query)}"
    _, payload = json_fetch(conn, kind="openalex_search", url=url, timeout_s=timeout_s)
    results = (payload or {}).get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        return None
    candidates = [build_openalex_candidate(item) for item in results if isinstance(item, dict)]
    candidate = choose_best_candidate(work, candidates)
    if candidate is not None:
        candidate["matched_by"] = "title_search"
    return candidate


def load_external_metadata(conn: sqlite3.Connection, work_id: int) -> dict[str, Any] | None:
    """Load stored external metadata for a work."""
    row = conn.execute(
        "SELECT * FROM external_metadata WHERE work_id = ?;",
        (work_id,),
    ).fetchone()
    if row is None:
        return None
    return dict(row)


def ensure_external_metadata(
    conn: sqlite3.Connection,
    *,
    work_id: int,
    timeout_s: int,
    refresh: bool = False,
) -> dict[str, Any]:
    """Ensure external metadata is available for a work."""
    ensure_policy_schema(conn)
    if not refresh:
        cached = load_external_metadata(conn, work_id)
        if cached is not None:
            return cached

    work = load_work_row(conn, work_id)
    candidates: list[dict[str, Any]] = []
    crossref_candidate = fetch_crossref_metadata(conn, work, timeout_s)
    if crossref_candidate is not None:
        candidates.append(crossref_candidate)
    openalex_candidate = fetch_openalex_metadata(conn, work, timeout_s)
    if openalex_candidate is not None:
        candidates.append(openalex_candidate)

    chosen = None
    if candidates:
        chosen = max(candidates, key=lambda item: float(item.get("match_score") or 0.0))

    if chosen is None:
        chosen = {
            "provider": "journal_ref" if work["journal_ref"] else "arxiv",
            "title": str(work["title"]),
            "doi": normalize_doi(work["doi"]),
            "url": work["abs_url"],
            "venue": work["journal_ref"] or "",
            "publisher": "",
            "source_type": "journal-ref" if work["journal_ref"] else "preprint",
            "published_year": parse_year(work["published"]),
            "is_formal_publication": False,
            "match_score": 40.0 if work["journal_ref"] else 20.0,
            "matched_by": "journal_ref" if work["journal_ref"] else "arxiv_only",
            "raw": None,
        }

    conn.execute(
        """
        INSERT INTO external_metadata(
          work_id, provider, canonical_title, canonical_doi, canonical_url, canonical_source_type,
          canonical_venue, publisher, published_year, is_formal_publication, match_score,
          matched_by, crossref_json, openalex_json, last_enriched_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(work_id) DO UPDATE SET
          provider=excluded.provider,
          canonical_title=excluded.canonical_title,
          canonical_doi=excluded.canonical_doi,
          canonical_url=excluded.canonical_url,
          canonical_source_type=excluded.canonical_source_type,
          canonical_venue=excluded.canonical_venue,
          publisher=excluded.publisher,
          published_year=excluded.published_year,
          is_formal_publication=excluded.is_formal_publication,
          match_score=excluded.match_score,
          matched_by=excluded.matched_by,
          crossref_json=excluded.crossref_json,
          openalex_json=excluded.openalex_json,
          last_enriched_at=excluded.last_enriched_at;
        """,
        (
            work_id,
            chosen.get("provider"),
            chosen.get("title"),
            normalize_doi(chosen.get("doi")),
            chosen.get("url"),
            chosen.get("source_type"),
            chosen.get("venue"),
            chosen.get("publisher"),
            chosen.get("published_year"),
            1 if chosen.get("is_formal_publication") else 0,
            float(chosen.get("match_score") or 0.0),
            chosen.get("matched_by"),
            json.dumps(crossref_candidate.get("raw"), ensure_ascii=False) if crossref_candidate is not None else None,
            json.dumps(openalex_candidate.get("raw"), ensure_ascii=False) if openalex_candidate is not None else None,
            now_iso(),
        ),
    )
    conn.commit()
    stored = load_external_metadata(conn, work_id)
    if stored is None:
        raise RuntimeError(f"external metadata missing after upsert: work_id={work_id}")
    return stored


def assess_work(
    conn: sqlite3.Connection,
    *,
    work_id: int,
    config: dict[str, Any],
    timeout_s: int,
    refresh: bool = False,
) -> dict[str, Any]:
    """Assess work quality and source tier."""
    ensure_policy_schema(conn)
    work = load_work_row(conn, work_id)
    metadata = ensure_external_metadata(conn, work_id=work_id, timeout_s=timeout_s, refresh=refresh)

    canonical_doi = normalize_doi(metadata.get("canonical_doi") or work["doi"])
    canonical_venue = str(metadata.get("canonical_venue") or work["journal_ref"] or "")
    source_type = str(metadata.get("canonical_source_type") or "")
    has_formal_version = bool(metadata.get("is_formal_publication"))

    if has_formal_version and canonical_doi:
        tier = "A"
    elif has_formal_version or canonical_doi or work["journal_ref"]:
        tier = "B"
    else:
        tier = "C"

    score = {"A": 70, "B": 50, "C": 25}[tier]
    reasons: list[str] = [f"tier={tier}"]

    completeness_bonus = 0
    if canonical_doi:
        completeness_bonus += 5
        reasons.append("doi")
    if canonical_venue:
        completeness_bonus += 5
        reasons.append("venue")
    preferred_url = str(metadata.get("canonical_url") or work["abs_url"] or "")
    if preferred_url:
        completeness_bonus += 5
        reasons.append("url")
    score += completeness_bonus

    year = parse_year(metadata.get("published_year") or work["published"])
    if year is not None:
        age = max(0, datetime_now_year() - year)
        if age <= 3:
            score += 10
            reasons.append("recent<=3y")
        elif age <= 5:
            score += 5
            reasons.append("recent<=5y")

    preferred_venues = [str(item) for item in (config.get("preferred_venues") or [])]
    target_venue = str(config.get("target_venue") or "")
    if target_venue and target_venue not in preferred_venues:
        preferred_venues = [target_venue] + preferred_venues
    if venue_matches(canonical_venue, preferred_venues):
        score += 5
        reasons.append("preferred_venue")

    if source_type and source_type in FORMAL_SOURCE_TYPES:
        reasons.append(source_type)
    elif source_type:
        reasons.append(f"type={source_type}")

    score = min(int(score), 100)
    assessment_reason = "; ".join(reasons)

    conn.execute(
        """
        INSERT INTO source_assessments(
          work_id, source_tier, quality_score, assessment_reason, has_formal_version,
          canonical_source_type, canonical_venue, preferred_citation_url, assessed_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(work_id) DO UPDATE SET
          source_tier=excluded.source_tier,
          quality_score=excluded.quality_score,
          assessment_reason=excluded.assessment_reason,
          has_formal_version=excluded.has_formal_version,
          canonical_source_type=excluded.canonical_source_type,
          canonical_venue=excluded.canonical_venue,
          preferred_citation_url=excluded.preferred_citation_url,
          assessed_at=excluded.assessed_at;
        """,
        (
            work_id,
            tier,
            score,
            assessment_reason,
            1 if has_formal_version else 0,
            metadata.get("canonical_source_type"),
            canonical_venue,
            preferred_url,
            now_iso(),
        ),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM source_assessments WHERE work_id = ?;", (work_id,)).fetchone()
    if row is None:
        raise RuntimeError(f"source assessment missing after upsert: work_id={work_id}")
    return dict(row)


def datetime_now_year() -> int:
    """Return the current local year."""
    return int(now_iso()[:4])


def resolve_work_ids(
    conn: sqlite3.Connection,
    *,
    arxiv_ids: list[str] | None = None,
    search_id: int | None = None,
) -> list[int]:
    """Resolve a set of work ids from arXiv IDs or a search."""
    work_ids: list[int] = []
    seen: set[int] = set()

    if search_id is not None:
        rows = conn.execute(
            "SELECT work_id FROM search_results WHERE search_id = ? ORDER BY position ASC;",
            (search_id,),
        ).fetchall()
        for row in rows:
            work_id = int(row["work_id"])
            if work_id not in seen:
                work_ids.append(work_id)
                seen.add(work_id)

    for raw in arxiv_ids or []:
        arxiv_id, _ = normalize_arxiv_id(raw)
        work_id = ensure_work(conn, arxiv_id=arxiv_id, timeout_s=20)
        if work_id is not None and work_id not in seen:
            work_ids.append(work_id)
            seen.add(work_id)

    if work_ids:
        return work_ids

    rows = conn.execute("SELECT work_id FROM works ORDER BY last_seen_at DESC, work_id DESC;").fetchall()
    return [int(row["work_id"]) for row in rows]


def load_project_config(project_dir: Path | None) -> dict[str, Any]:
    """Load project config when available."""
    if project_dir is None:
        return load_paper_config(Path("."))
    return load_paper_config(project_dir)
