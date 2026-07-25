#!/usr/bin/env python3
"""arXiv-first discovery + BibTeX retrieval with a local SQLite registry.

Motivation
----------
For ML/AI, arXiv is a practical primary source for discovery and (preprint) metadata. This
script provides a reproducible, de-duplicating registry so that:
  - discovery queries and results are persisted (auditable)
  - parsed paper metadata is normalized and upserted by arXiv ID (stable)
  - BibTeX retrieval is cached in the registry and can be exported later

Data source
-----------
We use the arXiv Atom API:
  https://export.arxiv.org/api/query

This returns Atom XML with:
  - feed-level OpenSearch metadata (totalResults, startIndex, itemsPerPage)
  - entry-level fields: id (abs URL), title, summary, published, updated, authors,
    category terms, and optional arXiv extensions like comment / journal_ref / doi.

SQLite schema (high level)
--------------------------
The registry is designed around "works" keyed by base arXiv ID (strip version vN).
We store:
  - works: normalized metadata per arXiv paper
  - authors/work_authors: author names with stable ordering per work
  - searches/search_results: discovery queries and the ranked results returned
  - bibtex: latest fetched BibTeX entry per work
  - fetches: lightweight log of remote fetches (kind/url/hash)

This is intentionally arXiv-first, but leaves room for later DOI-based merging.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared"))

from paper_utils import now_iso  # noqa: E402


ATOM_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
    "opensearch": "http://a9.com/-/spec/opensearch/1.1/",
}


SCHEMA_VERSION = "1"


@dataclass(frozen=True)
class ArxivSearchParams:
    query: str
    start: int
    max_results: int
    sort_by: str
    sort_order: str


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch_url(url: str, *, timeout_s: int) -> tuple[int | None, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": "latex-arxiv-skill/arxiv-registry"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            status = getattr(resp, "status", None)
            return status, resp.read()
    except urllib.error.HTTPError as e:
        # Preserve body if present to aid debugging.
        body = e.read() if hasattr(e, "read") else b""
        return getattr(e, "code", None), body
    except urllib.error.URLError:
        return None, b""


def normalize_arxiv_id(value: str) -> tuple[str, str]:
    """Return (base_id, versioned_id) from various arXiv ID inputs."""
    raw = value.strip()
    raw = re.sub(r"^arxiv:\s*", "", raw, flags=re.IGNORECASE)
    raw = raw.split("?", 1)[0].rstrip("/")
    if "/abs/" in raw:
        raw = raw.split("/abs/", 1)[1]
    if "/pdf/" in raw:
        raw = raw.split("/pdf/", 1)[1]
    if raw.endswith(".pdf"):
        raw = raw[: -len(".pdf")]
    raw = raw.strip()
    base = re.sub(r"v\d+$", "", raw)
    return base, raw


def arxiv_query_url(params: ArxivSearchParams) -> str:
    q = params.query.strip()
    if not q:
        raise ValueError("query must be non-empty")
    query = {
        "search_query": q,
        "start": str(params.start),
        "max_results": str(params.max_results),
        "sortBy": params.sort_by,
        "sortOrder": params.sort_order,
    }
    return f"https://export.arxiv.org/api/query?{urllib.parse.urlencode(query)}"


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_meta (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS works (
          work_id INTEGER PRIMARY KEY,
          arxiv_id TEXT NOT NULL UNIQUE,
          title TEXT NOT NULL,
          summary TEXT,
          published TEXT,
          updated TEXT,
          comment TEXT,
          primary_category TEXT,
          categories_json TEXT,
          abs_url TEXT,
          pdf_url TEXT,
          journal_ref TEXT,
          doi TEXT,
          created_at TEXT NOT NULL,
          last_seen_at TEXT NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS works_doi_unique
        ON works(doi) WHERE doi IS NOT NULL;

        CREATE TABLE IF NOT EXISTS authors (
          author_id INTEGER PRIMARY KEY,
          name TEXT NOT NULL UNIQUE
        );

        CREATE TABLE IF NOT EXISTS work_authors (
          work_id INTEGER NOT NULL REFERENCES works(work_id) ON DELETE CASCADE,
          author_id INTEGER NOT NULL REFERENCES authors(author_id) ON DELETE CASCADE,
          position INTEGER NOT NULL,
          PRIMARY KEY (work_id, position),
          UNIQUE (work_id, author_id)
        );

        CREATE TABLE IF NOT EXISTS searches (
          search_id INTEGER PRIMARY KEY,
          requested_at TEXT NOT NULL,
          query TEXT NOT NULL,
          url TEXT NOT NULL,
          start INTEGER NOT NULL,
          max_results INTEGER NOT NULL,
          sort_by TEXT,
          sort_order TEXT,
          total_results INTEGER,
          items_per_page INTEGER,
          start_index INTEGER,
          result_count INTEGER NOT NULL,
          raw_sha256 TEXT NOT NULL,
          raw_xml TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS searches_params_idx
        ON searches(query, start, max_results, sort_by, sort_order, requested_at);

        CREATE TABLE IF NOT EXISTS search_results (
          search_id INTEGER NOT NULL REFERENCES searches(search_id) ON DELETE CASCADE,
          position INTEGER NOT NULL,
          arxiv_id TEXT NOT NULL,
          arxiv_id_versioned TEXT NOT NULL,
          work_id INTEGER REFERENCES works(work_id) ON DELETE SET NULL,
          PRIMARY KEY (search_id, position)
        );

        CREATE TABLE IF NOT EXISTS bibtex (
          work_id INTEGER PRIMARY KEY REFERENCES works(work_id) ON DELETE CASCADE,
          fetched_at TEXT NOT NULL,
          source_url TEXT NOT NULL,
          sha256 TEXT NOT NULL,
          bibtex TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS fetches (
          fetch_id INTEGER PRIMARY KEY,
          fetched_at TEXT NOT NULL,
          kind TEXT NOT NULL,
          url TEXT NOT NULL,
          status INTEGER,
          sha256 TEXT,
          bytes INTEGER
        );

        CREATE TABLE IF NOT EXISTS citation_keys (
          work_id INTEGER PRIMARY KEY REFERENCES works(work_id) ON DELETE CASCADE,
          key TEXT NOT NULL UNIQUE,
          base_key TEXT NOT NULL,
          generated_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value;",
        ("schema_version", SCHEMA_VERSION),
    )
    conn.commit()


def record_fetch(conn: sqlite3.Connection, *, kind: str, url: str, status: int | None, body: bytes) -> None:
    conn.execute(
        "INSERT INTO fetches(fetched_at, kind, url, status, sha256, bytes) VALUES(?, ?, ?, ?, ?, ?);",
        (now_iso(), kind, url, status, sha256_bytes(body) if body else None, len(body)),
    )


def xml_text(node: ET.Element | None) -> str | None:
    if node is None or node.text is None:
        return None
    return " ".join(node.text.split())


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def resolve_db_path(args: argparse.Namespace) -> Path:
    """Resolve DB path from CLI args.

    Precedence: --db > --project-dir > cwd (notes/arxiv-registry.sqlite3).
    """
    if getattr(args, "db", None):
        return Path(args.db).expanduser().resolve()
    base_dir = Path(getattr(args, "project_dir", None) or ".").expanduser().resolve()
    return base_dir / "notes" / "arxiv-registry.sqlite3"


def parse_arxiv_feed(xml_bytes: bytes) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    root = ET.fromstring(xml_bytes)

    def find_text(path: str) -> str | None:
        return xml_text(root.find(path, ATOM_NS))

    meta: dict[str, Any] = {
        "updated": find_text("atom:updated"),
        "total_results": find_text("opensearch:totalResults"),
        "items_per_page": find_text("opensearch:itemsPerPage"),
        "start_index": find_text("opensearch:startIndex"),
    }

    entries: list[dict[str, Any]] = []
    for entry in root.findall("atom:entry", ATOM_NS):
        entry_id = xml_text(entry.find("atom:id", ATOM_NS)) or ""
        _, arxiv_id_versioned = normalize_arxiv_id(entry_id)
        arxiv_id, _ = normalize_arxiv_id(arxiv_id_versioned)

        title = xml_text(entry.find("atom:title", ATOM_NS)) or ""
        summary = xml_text(entry.find("atom:summary", ATOM_NS))
        published = xml_text(entry.find("atom:published", ATOM_NS))
        updated = xml_text(entry.find("atom:updated", ATOM_NS))

        comment = xml_text(entry.find("arxiv:comment", ATOM_NS))
        journal_ref = xml_text(entry.find("arxiv:journal_ref", ATOM_NS))
        doi = xml_text(entry.find("arxiv:doi", ATOM_NS))

        primary_category = None
        primary_node = entry.find("arxiv:primary_category", ATOM_NS)
        if primary_node is not None:
            primary_category = primary_node.attrib.get("term")

        categories = []
        for cat in entry.findall("atom:category", ATOM_NS):
            term = (cat.attrib.get("term") or "").strip()
            if term:
                categories.append(term)

        abs_url = None
        pdf_url = None
        for link in entry.findall("atom:link", ATOM_NS):
            href = (link.attrib.get("href") or "").strip()
            if not href:
                continue
            link_type = (link.attrib.get("type") or "").strip()
            rel = (link.attrib.get("rel") or "").strip()
            if abs_url is None and rel == "alternate" and link_type == "text/html":
                abs_url = href
            if pdf_url is None and link_type == "application/pdf":
                pdf_url = href

        authors = []
        for author in entry.findall("atom:author", ATOM_NS):
            name = xml_text(author.find("atom:name", ATOM_NS))
            if name:
                authors.append(name)

        entries.append(
            {
                "arxiv_id": arxiv_id,
                "arxiv_id_versioned": arxiv_id_versioned,
                "title": title,
                "summary": summary,
                "published": published,
                "updated": updated,
                "comment": comment,
                "journal_ref": journal_ref,
                "doi": doi,
                "primary_category": primary_category,
                "categories": categories,
                "abs_url": abs_url,
                "pdf_url": pdf_url,
                "authors": authors,
            }
        )
    return meta, entries


def upsert_work(conn: sqlite3.Connection, work: dict[str, Any]) -> int:
    now = now_iso()
    categories_json = json.dumps(work.get("categories") or [], ensure_ascii=False)
    sql = """
    INSERT INTO works(
      arxiv_id, title, summary, published, updated, comment, primary_category,
      categories_json, abs_url, pdf_url, journal_ref, doi, created_at, last_seen_at
    )
    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(arxiv_id) DO UPDATE SET
      title=excluded.title,
      summary=excluded.summary,
      published=excluded.published,
      updated=excluded.updated,
      comment=COALESCE(excluded.comment, works.comment),
      primary_category=excluded.primary_category,
      categories_json=excluded.categories_json,
      abs_url=COALESCE(excluded.abs_url, works.abs_url),
      pdf_url=COALESCE(excluded.pdf_url, works.pdf_url),
      journal_ref=COALESCE(excluded.journal_ref, works.journal_ref),
      doi=COALESCE(works.doi, excluded.doi),
      last_seen_at=excluded.last_seen_at
    """

    def run(doi_value: str | None) -> None:
        conn.execute(
            sql,
            (
                work["arxiv_id"],
                work["title"],
                work.get("summary"),
                work.get("published"),
                work.get("updated"),
                work.get("comment"),
                work.get("primary_category"),
                categories_json,
                work.get("abs_url"),
                work.get("pdf_url"),
                work.get("journal_ref"),
                doi_value,
                now,
                now,
            ),
        )

    try:
        run(work.get("doi"))
    except sqlite3.IntegrityError:
        # DOI is optional and can occasionally collide due to upstream metadata errors.
        run(None)

    row = conn.execute("SELECT work_id FROM works WHERE arxiv_id = ?;", (work["arxiv_id"],)).fetchone()
    if row is None:
        raise RuntimeError("upsert_work: missing work row after upsert")
    work_id = int(row["work_id"])

    # Replace authors to preserve ordering (simplest and deterministic).
    conn.execute("DELETE FROM work_authors WHERE work_id = ?;", (work_id,))
    seen_author_ids: set[int] = set()
    pos = 0
    for name in work.get("authors") or []:
        conn.execute(
            "INSERT INTO authors(name) VALUES(?) ON CONFLICT(name) DO NOTHING;",
            (name,),
        )
        author_row = conn.execute("SELECT author_id FROM authors WHERE name = ?;", (name,)).fetchone()
        if author_row is None:
            raise RuntimeError("author insert/select failed")
        author_id = int(author_row["author_id"])
        if author_id in seen_author_ids:
            continue
        seen_author_ids.add(author_id)
        conn.execute(
            "INSERT INTO work_authors(work_id, author_id, position) VALUES(?, ?, ?);",
            (work_id, author_id, pos),
        )
        pos += 1
    return work_id


def ensure_initialized(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version';").fetchone()
    if row is None:
        raise SystemExit("error: registry schema is missing; run `init` first")


def search_cache_hit(
    conn: sqlite3.Connection, *, params: ArxivSearchParams, cache_ttl_s: int
) -> tuple[int, dict[str, Any], list[dict[str, Any]]] | None:
    if cache_ttl_s <= 0:
        return None
    row = conn.execute(
        """
        SELECT search_id, requested_at, raw_xml
        FROM searches
        WHERE query = ?
          AND start = ?
          AND max_results = ?
          AND sort_by = ?
          AND sort_order = ?
        ORDER BY requested_at DESC
        LIMIT 1;
        """,
        (params.query, params.start, params.max_results, params.sort_by, params.sort_order),
    ).fetchone()
    if row is None:
        return None

    requested_at = parse_iso_datetime(row["requested_at"])
    if requested_at is None:
        return None
    age_s = (datetime.now(timezone.utc) - requested_at.astimezone(timezone.utc)).total_seconds()
    if age_s > float(cache_ttl_s):
        return None

    raw_xml = str(row["raw_xml"]).encode("utf-8", errors="replace")
    meta, entries = parse_arxiv_feed(raw_xml)
    return int(row["search_id"]), meta, entries


def surname_from_author(name: str) -> str:
    cleaned = " ".join(name.split()).strip()
    if not cleaned:
        return ""
    if "," in cleaned:
        return cleaned.split(",", 1)[0].strip()
    parts = cleaned.split()
    return parts[-1].strip()


def normalize_key_token(value: str) -> str:
    token = re.sub(r"[^a-z0-9]+", "", value.lower())
    return token.strip()


def first_title_token(title: str) -> str:
    for tok in re.split(r"[^A-Za-z0-9]+", title.strip()):
        if tok:
            return tok
    return ""


def year_from_published(published: str | None) -> str:
    if not published:
        return "0000"
    m = re.match(r"^(\d{4})", published.strip())
    return m.group(1) if m else "0000"


def ensure_citation_key(conn: sqlite3.Connection, *, work_id: int) -> str:
    row = conn.execute("SELECT key FROM citation_keys WHERE work_id = ?;", (work_id,)).fetchone()
    if row is not None:
        return str(row["key"])

    work = conn.execute(
        "SELECT arxiv_id, title, published FROM works WHERE work_id = ?;",
        (work_id,),
    ).fetchone()
    if work is None:
        raise RuntimeError(f"work not found: work_id={work_id}")

    author_row = conn.execute(
        """
        SELECT a.name
        FROM work_authors wa
        JOIN authors a ON a.author_id = wa.author_id
        WHERE wa.work_id = ?
        ORDER BY wa.position ASC
        LIMIT 1;
        """,
        (work_id,),
    ).fetchone()
    first_author = surname_from_author(str(author_row["name"])) if author_row is not None else ""
    year = year_from_published(work["published"])
    first_word = first_title_token(str(work["title"]))

    base = normalize_key_token(first_author) + year + normalize_key_token(first_word)
    if not base:
        base = "arxiv" + normalize_key_token(str(work["arxiv_id"]))

    candidate = base
    existing = conn.execute("SELECT work_id FROM citation_keys WHERE key = ?;", (candidate,)).fetchone()
    if existing is not None and int(existing["work_id"]) != int(work_id):
        digits = re.sub(r"\D+", "", str(work["arxiv_id"]))
        suffix = digits[-6:] if digits else str(work_id)
        candidate = f"{base}{suffix}"
        existing = conn.execute("SELECT work_id FROM citation_keys WHERE key = ?;", (candidate,)).fetchone()
        if existing is not None and int(existing["work_id"]) != int(work_id):
            candidate = f"{base}{work_id}"

    conn.execute(
        "INSERT INTO citation_keys(work_id, key, base_key, generated_at) VALUES(?, ?, ?, ?);",
        (work_id, candidate, base, now_iso()),
    )
    return candidate


def rewrite_bibtex_key(bibtex_text: str, new_key: str) -> str:
    match = re.search(r"@(\w+)\s*\{\s*([^,]+)\s*,", bibtex_text)
    if match is None:
        return bibtex_text
    start, end = match.span(2)
    return bibtex_text[:start] + new_key + bibtex_text[end:]


def ensure_work(conn: sqlite3.Connection, *, arxiv_id: str, timeout_s: int) -> int | None:
    row = conn.execute("SELECT work_id FROM works WHERE arxiv_id = ?;", (arxiv_id,)).fetchone()
    if row is not None:
        return int(row["work_id"])

    meta_url = f"https://export.arxiv.org/api/query?id_list={urllib.parse.quote(arxiv_id)}&max_results=1"
    status, body = fetch_url(meta_url, timeout_s=timeout_s)
    if not body:
        return None
    record_fetch(conn, kind="arxiv_api_id_list", url=meta_url, status=status, body=body)
    _, entries = parse_arxiv_feed(body)
    if not entries:
        return None
    return upsert_work(conn, entries[0])


def ensure_bibtex(
    conn: sqlite3.Connection, *, arxiv_id: str, work_id: int, timeout_s: int, refresh: bool
) -> str | None:
    row = conn.execute("SELECT bibtex FROM bibtex WHERE work_id = ?;", (work_id,)).fetchone()
    if row is not None and not refresh:
        return str(row["bibtex"])

    bib_url = f"https://arxiv.org/bibtex/{urllib.parse.quote(arxiv_id)}"
    status, body = fetch_url(bib_url, timeout_s=timeout_s)
    if not body:
        return None
    record_fetch(conn, kind="arxiv_bibtex", url=bib_url, status=status, body=body)

    bibtex_text = body.decode("utf-8", errors="replace").strip() + "\n"
    conn.execute(
        """
        INSERT INTO bibtex(work_id, fetched_at, source_url, sha256, bibtex)
        VALUES(?, ?, ?, ?, ?)
        ON CONFLICT(work_id) DO UPDATE SET
          fetched_at=excluded.fetched_at,
          source_url=excluded.source_url,
          sha256=excluded.sha256,
          bibtex=excluded.bibtex;
        """,
        (work_id, now_iso(), bib_url, sha256_bytes(body), bibtex_text),
    )
    return bibtex_text


def cmd_init(args: argparse.Namespace) -> int:
    db_path = resolve_db_path(args)
    with connect(db_path) as conn:
        init_schema(conn)
    print(f"Initialized registry: {db_path}")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    db_path = resolve_db_path(args)
    params = ArxivSearchParams(
        query=args.query,
        start=args.start,
        max_results=args.max_results,
        sort_by=args.sort_by,
        sort_order=args.sort_order,
    )
    url = arxiv_query_url(params)

    with connect(db_path) as conn:
        init_schema(conn)
        ensure_initialized(conn)

        if not args.refresh:
            cached = search_cache_hit(conn, params=params, cache_ttl_s=args.cache_ttl_s)
            if cached is not None:
                search_id, meta, entries = cached
                print(
                    f"Search cache hit: search_id={search_id} results={len(entries)} total={meta.get('total_results')}"
                )
                if args.print:
                    for entry in entries[: args.print]:
                        print(f"- {entry['arxiv_id_versioned']} | {entry['title']}")
                return 0

    status, body = fetch_url(url, timeout_s=args.timeout_s)
    if not body:
        print(f"error: empty response (status={status}) from {url}", file=sys.stderr)
        return 2

    meta, entries = parse_arxiv_feed(body)

    with connect(db_path) as conn:
        init_schema(conn)
        ensure_initialized(conn)
        record_fetch(conn, kind="arxiv_api_search", url=url, status=status, body=body)

        cursor = conn.execute(
            """
            INSERT INTO searches(
              requested_at, query, url, start, max_results, sort_by, sort_order,
              total_results, items_per_page, start_index, result_count, raw_sha256, raw_xml
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now_iso(),
                params.query,
                url,
                params.start,
                params.max_results,
                params.sort_by,
                params.sort_order,
                int(meta["total_results"]) if meta.get("total_results") and str(meta["total_results"]).isdigit() else None,
                int(meta["items_per_page"]) if meta.get("items_per_page") and str(meta["items_per_page"]).isdigit() else None,
                int(meta["start_index"]) if meta.get("start_index") and str(meta["start_index"]).isdigit() else None,
                len(entries),
                sha256_bytes(body),
                body.decode("utf-8", errors="replace"),
            ),
        )
        if cursor.lastrowid is None:
            raise RuntimeError("search insert failed")
        search_id = int(cursor.lastrowid)

        for pos, entry in enumerate(entries):
            work_id = upsert_work(conn, entry)
            conn.execute(
                "INSERT INTO search_results(search_id, position, arxiv_id, arxiv_id_versioned, work_id) "
                "VALUES(?, ?, ?, ?, ?);",
                (search_id, pos, entry["arxiv_id"], entry["arxiv_id_versioned"], work_id),
            )

        conn.commit()

    print(f"Search stored: search_id={search_id} results={len(entries)} total={meta.get('total_results')}")
    if args.print:
        for entry in entries[: args.print]:
            print(f"- {entry['arxiv_id_versioned']} | {entry['title']}")
    return 0


def cmd_fetch_bibtex(args: argparse.Namespace) -> int:
    db_path = resolve_db_path(args)
    arxiv_inputs = [s.strip() for s in args.arxiv_id if s.strip()]
    if not arxiv_inputs:
        print("error: provide at least one arXiv id", file=sys.stderr)
        return 2

    with connect(db_path) as conn:
        init_schema(conn)
        ensure_initialized(conn)

        for raw in arxiv_inputs:
            arxiv_id, _ = normalize_arxiv_id(raw)
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

            if args.out_bib:
                out_path = Path(args.out_bib).resolve()
                out_path.parent.mkdir(parents=True, exist_ok=True)
                with out_path.open("a", encoding="utf-8") as f:
                    f.write(bibtex_text.rstrip() + "\n\n")

            if args.print_bibtex:
                print(bibtex_text.rstrip())
                print()

            if args.sleep_s > 0:
                time.sleep(args.sleep_s)

        conn.commit()

    print("BibTeX fetch complete.")
    return 0


def work_payload(conn: sqlite3.Connection, *, work_id: int, ensure_key: bool) -> dict[str, Any]:
    work = conn.execute(
        """
        SELECT work_id, arxiv_id, title, summary, published, updated, comment, primary_category,
               categories_json, abs_url, pdf_url, journal_ref, doi
        FROM works
        WHERE work_id = ?;
        """,
        (work_id,),
    ).fetchone()
    if work is None:
        raise RuntimeError(f"work not found: work_id={work_id}")

    authors = [
        str(r["name"])
        for r in conn.execute(
            """
            SELECT a.name
            FROM work_authors wa
            JOIN authors a ON a.author_id = wa.author_id
            WHERE wa.work_id = ?
            ORDER BY wa.position ASC;
            """,
            (work_id,),
        ).fetchall()
    ]

    categories_json = work["categories_json"]
    categories = json.loads(categories_json) if categories_json else []

    bib_row = conn.execute(
        "SELECT fetched_at, source_url, sha256 FROM bibtex WHERE work_id = ?;",
        (work_id,),
    ).fetchone()

    cite_key = ensure_citation_key(conn, work_id=work_id) if ensure_key else None

    payload: dict[str, Any] = {
        "work_id": int(work["work_id"]),
        "arxiv_id": str(work["arxiv_id"]),
        "title": str(work["title"]),
        "authors": authors,
        "published": work["published"],
        "updated": work["updated"],
        "summary": work["summary"],
        "comment": work["comment"],
        "primary_category": work["primary_category"],
        "categories": categories,
        "abs_url": work["abs_url"],
        "pdf_url": work["pdf_url"],
        "journal_ref": work["journal_ref"],
        "doi": work["doi"],
        "citation_key": cite_key,
        "bibtex_cached": bib_row is not None,
        "bibtex_fetched_at": bib_row["fetched_at"] if bib_row is not None else None,
        "bibtex_source_url": bib_row["source_url"] if bib_row is not None else None,
        "bibtex_sha256": bib_row["sha256"] if bib_row is not None else None,
    }
    return payload


def cmd_get(args: argparse.Namespace) -> int:
    db_path = resolve_db_path(args)
    inputs = [s.strip() for s in args.arxiv_id if s.strip()]
    if not inputs:
        print("error: provide at least one arXiv id", file=sys.stderr)
        return 2

    with connect(db_path) as conn:
        init_schema(conn)
        ensure_initialized(conn)

        for raw in inputs:
            arxiv_id, _ = normalize_arxiv_id(raw)
            work_id = conn.execute("SELECT work_id FROM works WHERE arxiv_id = ?;", (arxiv_id,)).fetchone()
            resolved_id = arxiv_id
            if work_id is None and args.fetch_missing:
                new_work_id = ensure_work(conn, arxiv_id=arxiv_id, timeout_s=args.timeout_s)
                if new_work_id is not None:
                    work_id = {"work_id": new_work_id}
            if work_id is None:
                print(json.dumps({"arxiv_id": resolved_id, "error": "not found"}, ensure_ascii=False))
                continue
            payload = work_payload(conn, work_id=int(work_id["work_id"]), ensure_key=args.ensure_key)
            print(json.dumps(payload, ensure_ascii=False))

        conn.commit()
    return 0


def existing_bibtex_keys(path: Path) -> set[str]:
    if not path.exists():
        return set()
    text = path.read_text(encoding="utf-8", errors="replace")
    return set(re.findall(r"@\w+\s*\{\s*([^,\s]+)\s*,", text))


def cmd_export_bibtex(args: argparse.Namespace) -> int:
    db_path = resolve_db_path(args)
    out_path = Path(args.out_bib).resolve() if args.out_bib else None

    arxiv_ids: list[str] = []
    if args.search_id is not None:
        with connect(db_path) as conn:
            init_schema(conn)
            ensure_initialized(conn)
            rows = conn.execute(
                """
                SELECT arxiv_id
                FROM search_results
                WHERE search_id = ?
                ORDER BY position ASC;
                """,
                (args.search_id,),
            ).fetchall()
            arxiv_ids.extend([str(r["arxiv_id"]) for r in rows])

    if args.arxiv_id:
        arxiv_ids.extend([normalize_arxiv_id(s)[0] for s in args.arxiv_id if s.strip()])

    arxiv_ids = [a for a in arxiv_ids if a]
    if not arxiv_ids:
        print("error: provide arXiv IDs or --search-id", file=sys.stderr)
        return 2

    keys_in_file: set[str] = set()
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        keys_in_file = existing_bibtex_keys(out_path)

    exported = 0
    skipped = 0

    with connect(db_path) as conn:
        init_schema(conn)
        ensure_initialized(conn)

        for arxiv_id in arxiv_ids:
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
            rewritten = rewrite_bibtex_key(bibtex_text, cite_key).rstrip() + "\n"

            if out_path is None:
                print(rewritten)
                if args.sleep_s > 0:
                    time.sleep(args.sleep_s)
                continue

            if cite_key in keys_in_file:
                skipped += 1
            else:
                with out_path.open("a", encoding="utf-8") as f:
                    f.write(rewritten + "\n")
                keys_in_file.add(cite_key)
                exported += 1

            if args.sleep_s > 0:
                time.sleep(args.sleep_s)

        conn.commit()

    if out_path is not None:
        print(f"BibTeX export complete: wrote={exported} skipped={skipped} path={out_path}")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    db_path = resolve_db_path(args)
    with connect(db_path) as conn:
        ensure_initialized(conn)
        works = conn.execute("SELECT COUNT(1) AS n FROM works;").fetchone()["n"]
        searches = conn.execute("SELECT COUNT(1) AS n FROM searches;").fetchone()["n"]
        bib = conn.execute("SELECT COUNT(1) AS n FROM bibtex;").fetchone()["n"]
        print(f"works={works} searches={searches} bibtex={bib}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="arXiv-first discovery + BibTeX registry (SQLite).")
    parser.add_argument(
        "--project-dir",
        help="Paper/project directory; defaults DB to <project-dir>/notes/arxiv-registry.sqlite3.",
    )
    parser.add_argument(
        "--db",
        help="Registry sqlite path (overrides --project-dir).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="Initialize the registry schema.")
    p_init.set_defaults(fn=cmd_init)

    p_search = sub.add_parser("search", help="Run an arXiv API search and store results.")
    p_search.add_argument("query", help='arXiv API search query (e.g., all:"world model" AND cat:cs.LG).')
    p_search.add_argument("--start", type=int, default=0, help="Start offset (default: 0).")
    p_search.add_argument("--max-results", type=int, default=25, help="Max results (default: 25).")
    p_search.add_argument(
        "--sort-by",
        default="relevance",
        choices=["relevance", "lastUpdatedDate", "submittedDate"],
        help="Sort criterion (default: relevance).",
    )
    p_search.add_argument(
        "--sort-order",
        default="descending",
        choices=["ascending", "descending"],
        help="Sort order (default: descending).",
    )
    p_search.add_argument("--timeout-s", type=int, default=20, help="Network timeout seconds (default: 20).")
    p_search.add_argument(
        "--cache-ttl-s",
        type=int,
        default=86400,
        help="Use cached identical search results if younger than this many seconds (default: 86400; set 0 to disable).",
    )
    p_search.add_argument(
        "--refresh",
        action="store_true",
        help="Force a network fetch (bypass search cache).",
    )
    p_search.add_argument(
        "--print",
        type=int,
        default=5,
        help="Print first N results to stdout (default: 5; set 0 to disable).",
    )
    p_search.set_defaults(fn=cmd_search)

    p_bib = sub.add_parser("fetch-bibtex", help="Fetch arXiv BibTeX for one or more arXiv IDs.")
    p_bib.add_argument("arxiv_id", nargs="+", help="arXiv IDs (e.g., 2301.04104 or https://arxiv.org/abs/2301.04104v2).")
    p_bib.add_argument("--timeout-s", type=int, default=20, help="Network timeout seconds (default: 20).")
    p_bib.add_argument("--refresh", action="store_true", help="Force a network fetch (ignore cached BibTeX).")
    p_bib.add_argument("--sleep-s", type=float, default=0.0, help="Sleep between requests (default: 0).")
    p_bib.add_argument("--print-bibtex", action="store_true", help="Print the BibTeX entries to stdout.")
    p_bib.add_argument(
        "--out-bib",
        help="Optional path to append fetched BibTeX into a .bib file (no de-dup here; DB remains canonical).",
    )
    p_bib.set_defaults(fn=cmd_fetch_bibtex)

    p_get = sub.add_parser("get", help="Get cached metadata for one or more arXiv IDs (JSON lines).")
    p_get.add_argument("arxiv_id", nargs="+", help="arXiv IDs (e.g., 2301.04104 or https://arxiv.org/abs/2301.04104v2).")
    p_get.add_argument("--timeout-s", type=int, default=20, help="Network timeout seconds (default: 20).")
    p_get.add_argument("--fetch-missing", action="store_true", help="Fetch metadata from arXiv if missing from the registry.")
    p_get.add_argument("--ensure-key", action="store_true", help="Ensure a citation key exists for each work (stored in DB).")
    p_get.set_defaults(fn=cmd_get)

    p_export = sub.add_parser("export-bibtex", help="Export BibTeX with stable citation keys.")
    p_export.add_argument("arxiv_id", nargs="*", help="arXiv IDs to export (optional if using --search-id).")
    p_export.add_argument("--search-id", type=int, help="Export all results from a stored search_id.")
    p_export.add_argument("--out-bib", help="Append entries to this .bib file (skips existing keys). If omitted, prints to stdout.")
    p_export.add_argument("--timeout-s", type=int, default=20, help="Network timeout seconds (default: 20).")
    p_export.add_argument("--refresh", action="store_true", help="Force a network fetch (ignore cached BibTeX).")
    p_export.add_argument("--sleep-s", type=float, default=0.0, help="Sleep between requests (default: 0).")
    p_export.set_defaults(fn=cmd_export_bibtex)

    p_stats = sub.add_parser("stats", help="Print basic registry stats.")
    p_stats.set_defaults(fn=cmd_stats)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
