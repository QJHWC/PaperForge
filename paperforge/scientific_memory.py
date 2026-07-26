from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from engine.secret_redaction import contains_secret

from .models import ClaimRelation, ClaimStatus, ClaimType, utc_now
from .path_safety import (
    UnsafePathError,
    is_link_or_reparse_point,
    reject_symlink_components,
    safe_mkdir,
)

SCHEMA_VERSION = 3
PUBLICATION_STATUSES = {
    ClaimStatus.SUPPORTED_STATIC.value,
    ClaimStatus.VERIFIED_RUNTIME.value,
    ClaimStatus.VERIFIED_EXPERIMENT.value,
}
NON_CLAIM_CATEGORIES = frozenset(
    {"acknowledgment", "author_metadata", "formatting"}
)
_ACKNOWLEDGMENT_PATTERN = re.compile(
    r"(?i)^(?:acknowledg(?:e)?ments?|致谢)[.:：。]?$"
)
_AUTHOR_METADATA_PATTERN = re.compile(
    r"(?i)^(?:authors?|affiliations?|correspondence|email)[.:：。]?$"
)
_FORMATTING_PATTERN = re.compile(
    r"(?i)^(?:appendix|supplementary material|references|bibliography)\.?$"
)
_SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")


def valid_non_claim_structure(text: str, category: str) -> bool:
    normalized = str(text).strip()
    if category == "acknowledgment":
        return bool(_ACKNOWLEDGMENT_PATTERN.fullmatch(normalized))
    if category == "author_metadata":
        return bool(_AUTHOR_METADATA_PATTERN.fullmatch(normalized))
    if category == "formatting":
        return bool(_FORMATTING_PATTERN.fullmatch(normalized))
    return False


def valid_non_claim_metadata(text: str, metadata: Mapping[str, Any]) -> bool:
    category = str(metadata.get("non_claim_category", "")).strip()
    review_id = str(metadata.get("non_claim_review_id", "")).strip()
    if category not in NON_CLAIM_CATEGORIES or not review_id.startswith(
        "claim_review_"
    ):
        return False
    return valid_non_claim_structure(text, category)


def _review_matches_non_claim(
    row: sqlite3.Row | None,
    *,
    text: str,
    category: str,
) -> bool:
    return bool(
        row is not None
        and row["decision"] == "APPROVED"
        and row["category"] == category
        and row["claim_text_sha256"]
        == hashlib.sha256(str(text).strip().encode("utf-8")).hexdigest()
    )


def _json(value: Mapping[str, Any] | Sequence[Any] | None) -> str:
    if contains_secret(value or {}):
        raise ValueError("scientific memory metadata must not contain credentials")
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True)


def _stable_id(prefix: str, *parts: object) -> str:
    digest = hashlib.sha256("\x1f".join(str(part) for part in parts).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:24]}"


class ScientificMemory:
    def __init__(
        self,
        path: str | Path,
        *,
        trusted_root: str | Path | None = None,
    ) -> None:
        lexical = Path(path).expanduser()
        if not lexical.is_absolute():
            lexical = Path.cwd() / lexical
        lexical = lexical.absolute()
        if trusted_root is not None:
            lexical_anchor = Path(trusted_root).expanduser()
            if not lexical_anchor.is_absolute():
                lexical_anchor = Path.cwd() / lexical_anchor
            lexical_anchor = lexical_anchor.absolute()
            anchor = lexical_anchor.resolve(strict=True)
            try:
                relative = lexical.relative_to(lexical_anchor)
            except ValueError:
                resolved_parent = lexical.parent.resolve(strict=True)
                lexical = resolved_parent / lexical.name
            else:
                lexical = anchor / relative
        else:
            if is_link_or_reparse_point(lexical.parent):
                raise UnsafePathError(
                    f"writable path contains a symbolic link: {lexical.parent}"
                )
            # Fresh publication workspaces may not have their state parent yet.
            # Establish that directory from the nearest existing safe ancestor
            # before treating it as the durable no-link boundary.
            anchor = safe_mkdir(lexical.parent)
            lexical = anchor / lexical.name
        safe_mkdir(lexical.parent, anchor=anchor)
        reject_symlink_components(lexical, anchor=anchor)
        self._trusted_anchor = anchor
        self.path = lexical
        self._initialize()

    def _validate_database_path(self) -> None:
        reject_symlink_components(self.path, anchor=self._trusted_anchor)
        candidates = (
            self.path,
            Path(f"{self.path}-wal"),
            Path(f"{self.path}-shm"),
            Path(f"{self.path}-journal"),
        )
        for candidate in candidates:
            reject_symlink_components(candidate, anchor=self._trusted_anchor)
            if is_link_or_reparse_point(candidate):
                raise UnsafePathError(
                    f"scientific memory path is a link or reparse point: {candidate}"
                )
            if candidate.exists() and not candidate.is_file():
                raise UnsafePathError(
                    f"scientific memory path is not a regular file: {candidate}"
                )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self._validate_database_path()
        uri = f"{self.path.as_uri()}?mode=rwc&nofollow=1"
        connection = sqlite3.connect(uri, timeout=30, uri=True)
        try:
            self._validate_database_path()
        except Exception:
            connection.close()
            raise
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sources (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    uri TEXT NOT NULL,
                    commit_sha TEXT,
                    path TEXT,
                    blob_sha256 TEXT,
                    content_sha256 TEXT,
                    license_id TEXT,
                    notice_sha256 TEXT,
                    captured_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evidence (
                    id TEXT PRIMARY KEY,
                    evidence_type TEXT NOT NULL,
                    source_id TEXT,
                    path TEXT,
                    line_start INTEGER,
                    line_end INTEGER,
                    excerpt TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    config_scope TEXT,
                    captured_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    FOREIGN KEY(source_id) REFERENCES sources(id)
                );
                CREATE TABLE IF NOT EXISTS claims (
                    id TEXT PRIMARY KEY,
                    claim_type TEXT NOT NULL,
                    text TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS claim_evidence (
                    claim_id TEXT NOT NULL,
                    evidence_id TEXT NOT NULL,
                    relation TEXT NOT NULL,
                    PRIMARY KEY(claim_id, evidence_id, relation),
                    FOREIGN KEY(claim_id) REFERENCES claims(id) ON DELETE CASCADE,
                    FOREIGN KEY(evidence_id) REFERENCES evidence(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS experiments (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    proposal_json TEXT NOT NULL,
                    approved_at TEXT,
                    cost_limit REAL,
                    risk_level TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    experiment_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    status TEXT NOT NULL,
                    code_sha256 TEXT,
                    config_sha256 TEXT,
                    data_sha256 TEXT,
                    checkpoint_sha256 TEXT,
                    metrics_sha256 TEXT,
                    started_at TEXT,
                    ended_at TEXT,
                    metadata_json TEXT NOT NULL,
                    FOREIGN KEY(experiment_id) REFERENCES experiments(id)
                );
                CREATE TABLE IF NOT EXISTS experiment_budget_events (
                    id TEXT PRIMARY KEY,
                    experiment_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    amount REAL NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    FOREIGN KEY(experiment_id) REFERENCES experiments(id)
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY,
                    run_id TEXT,
                    kind TEXT NOT NULL,
                    path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    tier TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(id)
                );
                CREATE TABLE IF NOT EXISTS reviews (
                    id TEXT PRIMARY KEY,
                    artifact_id TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    comments TEXT NOT NULL,
                    reviewer TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(artifact_id) REFERENCES artifacts(id)
                );
                CREATE TABLE IF NOT EXISTS workflows (
                    id TEXT PRIMARY KEY,
                    profile TEXT NOT NULL,
                    status TEXT NOT NULL,
                    workspace TEXT NOT NULL,
                    checkpoint TEXT,
                    started_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    error_code TEXT,
                    version INTEGER NOT NULL DEFAULT 0,
                    metadata_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workflow_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    workflow_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    idempotency_key TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(workflow_id, idempotency_key),
                    FOREIGN KEY(workflow_id) REFERENCES workflows(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY,
                    proposal_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    approved_by TEXT NOT NULL,
                    approved_at TEXT NOT NULL,
                    scope_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS claim_reviews (
                    id TEXT PRIMARY KEY,
                    claim_text_sha256 TEXT NOT NULL,
                    category TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    reviewer TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL
                );
                """
            )
            workflow_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(workflows)").fetchall()
            }
            if "version" not in workflow_columns:
                db.execute(
                    "ALTER TABLE workflows ADD COLUMN version INTEGER NOT NULL DEFAULT 0"
                )
            db.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    def add_source(
        self,
        *,
        kind: str,
        uri: str,
        commit_sha: str | None = None,
        path: str | None = None,
        blob_sha256: str | None = None,
        content_sha256: str | None = None,
        license_id: str | None = None,
        notice_sha256: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> str:
        for label, digest in (
            ("blob_sha256", blob_sha256),
            ("content_sha256", content_sha256),
            ("notice_sha256", notice_sha256),
        ):
            if digest is not None and not _SHA256_PATTERN.fullmatch(str(digest)):
                raise ValueError(f"{label} must be a 64-character hexadecimal SHA-256")
        if contains_secret(
            {
                "kind": kind,
                "uri": uri,
                "commit_sha": commit_sha,
                "path": path,
                "license_id": license_id,
                "metadata": dict(metadata or {}),
            }
        ):
            raise ValueError("scientific source must not contain credentials")
        source_id = _stable_id("src", kind, uri, commit_sha or "", path or "", blob_sha256 or "")
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO sources
                (id, kind, uri, commit_sha, path, blob_sha256, content_sha256,
                 license_id, notice_sha256, captured_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    kind=excluded.kind,
                    uri=excluded.uri,
                    commit_sha=excluded.commit_sha,
                    path=excluded.path,
                    blob_sha256=excluded.blob_sha256,
                    content_sha256=excluded.content_sha256,
                    license_id=excluded.license_id,
                    notice_sha256=excluded.notice_sha256,
                    captured_at=excluded.captured_at,
                    metadata_json=excluded.metadata_json
                """,
                (
                    source_id,
                    kind,
                    uri,
                    commit_sha,
                    path,
                    blob_sha256,
                    content_sha256,
                    license_id,
                    notice_sha256,
                    utc_now(),
                    _json(metadata),
                ),
            )
        return source_id

    def add_evidence(
        self,
        *,
        evidence_type: str,
        excerpt: str,
        source_id: str | None = None,
        path: str | None = None,
        line_start: int | None = None,
        line_end: int | None = None,
        config_scope: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> str:
        if contains_secret(
            {
                "evidence_type": evidence_type,
                "excerpt": excerpt,
                "source_id": source_id,
                "path": path,
                "config_scope": config_scope,
                "metadata": dict(metadata or {}),
            }
        ):
            raise ValueError("scientific evidence must not contain credentials")
        content_sha = hashlib.sha256(excerpt.encode("utf-8")).hexdigest()
        evidence_id = _stable_id(
            "ev",
            evidence_type,
            source_id or "",
            path or "",
            line_start or "",
            line_end or "",
            content_sha,
        )
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO evidence
                (id, evidence_type, source_id, path, line_start, line_end, excerpt,
                 content_sha256, config_scope, captured_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    evidence_type=excluded.evidence_type,
                    source_id=excluded.source_id,
                    path=excluded.path,
                    line_start=excluded.line_start,
                    line_end=excluded.line_end,
                    excerpt=excluded.excerpt,
                    content_sha256=excluded.content_sha256,
                    config_scope=excluded.config_scope,
                    captured_at=excluded.captured_at,
                    metadata_json=excluded.metadata_json
                """,
                (
                    evidence_id,
                    evidence_type,
                    source_id,
                    path,
                    line_start,
                    line_end,
                    excerpt,
                    content_sha,
                    config_scope,
                    utc_now(),
                    _json(metadata),
                ),
            )
        return evidence_id

    def approve_non_claim(
        self,
        *,
        text: str,
        category: str,
        reviewer: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> str:
        normalized_text = str(text).strip()
        normalized_category = str(category).strip()
        normalized_reviewer = str(reviewer).strip()
        if contains_secret(
            {
                "text": normalized_text,
                "reviewer": normalized_reviewer,
                "metadata": dict(metadata or {}),
            }
        ):
            raise ValueError("NON_CLAIM review must not contain credentials")
        if (
            normalized_category not in NON_CLAIM_CATEGORIES
            or not normalized_reviewer
            or not valid_non_claim_structure(normalized_text, normalized_category)
        ):
            raise ValueError("NON_CLAIM review does not match a controlled structure")
        text_sha256 = hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()
        review_id = _stable_id(
            "claim_review",
            text_sha256,
            normalized_category,
            normalized_reviewer,
        )
        with self.connect() as db:
            db.execute(
                """
                INSERT OR REPLACE INTO claim_reviews
                (id, claim_text_sha256, category, decision, reviewer, created_at,
                 metadata_json)
                VALUES (?, ?, ?, 'APPROVED', ?, ?, ?)
                """,
                (
                    review_id,
                    text_sha256,
                    normalized_category,
                    normalized_reviewer,
                    utc_now(),
                    _json(metadata),
                ),
            )
        return review_id

    def _non_claim_review_valid(
        self,
        *,
        text: str,
        category: str,
        review_id: str,
    ) -> bool:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM claim_reviews WHERE id = ?",
                (review_id,),
            ).fetchone()
        return bool(
            row is not None
            and row["decision"] == "APPROVED"
            and row["category"] == category
            and row["claim_text_sha256"]
            == hashlib.sha256(str(text).strip().encode("utf-8")).hexdigest()
        )

    def add_claim(
        self,
        *,
        claim_type: ClaimType | str,
        text: str,
        status: ClaimStatus | str,
        metadata: Mapping[str, Any] | None = None,
    ) -> str:
        normalized_type = ClaimType(claim_type).value
        normalized_status = ClaimStatus(status).value
        normalized_metadata = dict(metadata or {})
        if contains_secret(
            {"text": text, "metadata": normalized_metadata}
        ):
            raise ValueError("scientific claim must not contain credentials")
        if normalized_type == ClaimType.NON_CLAIM.value:
            category = str(
                normalized_metadata.get("non_claim_category", "")
            ).strip()
            review_id = str(
                normalized_metadata.get("non_claim_review_id", "")
            ).strip()
            if (
                normalized_status != ClaimStatus.NON_CLAIM.value
                or not valid_non_claim_metadata(text, normalized_metadata)
                or not self._non_claim_review_valid(
                    text=text,
                    category=category,
                    review_id=review_id,
                )
            ):
                raise ValueError(
                    "NON_CLAIM records require status=NON_CLAIM and "
                    "an approved controlled category matching the text"
                )
        elif normalized_status == ClaimStatus.NON_CLAIM.value:
            raise ValueError("status=NON_CLAIM requires claim_type=NON_CLAIM")
        claim_id = _stable_id("claim", normalized_type, text)
        now = utc_now()
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO claims
                (id, claim_type, text, status, created_at, updated_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status=excluded.status,
                    updated_at=excluded.updated_at,
                    metadata_json=excluded.metadata_json
                """,
                (
                    claim_id,
                    normalized_type,
                    text,
                    normalized_status,
                    now,
                    now,
                    _json(normalized_metadata),
                ),
            )
        return claim_id

    def link_claim(
        self,
        claim_id: str,
        evidence_id: str,
        relation: ClaimRelation | str = ClaimRelation.SUPPORTS,
    ) -> None:
        with self.connect() as db:
            db.execute(
                """
                INSERT OR IGNORE INTO claim_evidence(claim_id, evidence_id, relation)
                VALUES (?, ?, ?)
                """,
                (claim_id, evidence_id, ClaimRelation(relation).value),
            )

    def claim_gate(self, *, final_publication: bool = True) -> dict[str, Any]:
        failures: list[dict[str, str]] = []
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT c.id, c.claim_type, c.text, c.status, c.metadata_json,
                       SUM(CASE WHEN ce.relation IN ('supports', 'qualifies') THEN 1 ELSE 0 END)
                           AS supporting_edges,
                       SUM(CASE WHEN ce.relation = 'contradicts' THEN 1 ELSE 0 END)
                           AS contradictions
                FROM claims c
                LEFT JOIN claim_evidence ce ON ce.claim_id = c.id
                GROUP BY c.id
                ORDER BY c.id
                """
            ).fetchall()
            review_rows = db.execute(
                "SELECT * FROM claim_reviews WHERE decision = 'APPROVED'"
            ).fetchall()
        reviews = {str(row["id"]): row for row in review_rows}
        public_claim_count = 0
        non_claim_count = 0
        for row in rows:
            status = str(row["status"])
            if row["claim_type"] == ClaimType.NON_CLAIM.value:
                non_claim_count += 1
                try:
                    metadata = json.loads(row["metadata_json"])
                except (TypeError, json.JSONDecodeError):
                    metadata = {}
                if (
                    status != ClaimStatus.NON_CLAIM.value
                    or not isinstance(metadata, Mapping)
                    or not valid_non_claim_metadata(str(row["text"]), metadata)
                    or not _review_matches_non_claim(
                        reviews.get(str(metadata.get("non_claim_review_id", ""))),
                        text=str(row["text"]),
                        category=str(metadata.get("non_claim_category", "")),
                    )
                ):
                    failures.append(
                        {
                            "claim_id": row["id"],
                            "reason": "invalid NON_CLAIM classification",
                        }
                    )
                continue
            public_claim_count += 1
            if final_publication and status not in PUBLICATION_STATUSES:
                failures.append({"claim_id": row["id"], "reason": f"status={status}"})
                continue
            if int(row["supporting_edges"] or 0) == 0:
                failures.append({"claim_id": row["id"], "reason": "no supporting evidence"})
                continue
            if int(row["contradictions"] or 0) > 0:
                failures.append({"claim_id": row["id"], "reason": "contradicted"})
                continue
            if row["claim_type"] == ClaimType.EXPERIMENT_RESULT.value:
                with self.connect() as db:
                    evidence_rows = db.execute(
                        """
                        SELECT e.evidence_type, e.metadata_json
                        FROM evidence e
                        JOIN claim_evidence ce ON ce.evidence_id = e.id
                        WHERE ce.claim_id = ?
                          AND ce.relation IN ('supports', 'qualifies')
                        """,
                        (row["id"],),
                    ).fetchall()
                    runs = {
                        run["id"]: run
                        for run in db.execute(
                            "SELECT id, status, metrics_sha256, metadata_json FROM runs"
                        ).fetchall()
                    }
                verified = False
                for evidence in evidence_rows:
                    if evidence["evidence_type"] != "EXPERIMENT_METRIC":
                        continue
                    try:
                        metadata = json.loads(evidence["metadata_json"])
                    except (TypeError, json.JSONDecodeError):
                        continue
                    run = runs.get(metadata.get("run_id"))
                    if run is None:
                        continue
                    try:
                        run_metadata = json.loads(run["metadata_json"])
                    except (TypeError, json.JSONDecodeError):
                        continue
                    verified = all(
                        (
                            metadata.get("execution_verified") is True,
                            metadata.get("eligible_for_claims") is True,
                            run_metadata.get("execution_verified") is True,
                            run_metadata.get("eligible_for_claims") is True,
                            run["status"] == "PASSED",
                            bool(run["metrics_sha256"]),
                            metadata.get("metrics_sha256")
                            == run["metrics_sha256"],
                        )
                    )
                    if verified:
                        break
                if not verified:
                    failures.append(
                        {
                            "claim_id": row["id"],
                            "reason": "no execution-verified experiment evidence",
                        }
                    )
        if public_claim_count == 0:
            failures.append(
                {
                    "claim_id": "<manifest>",
                    "reason": "no public claims",
                }
            )
        return {
            "passed": public_claim_count > 0 and not failures,
            "claim_count": len(rows),
            "public_claim_count": public_claim_count,
            "non_claim_count": non_claim_count,
            "failures": failures,
        }

    def claim_manifest(self, tex_spans: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
        with self.connect() as db:
            claims = db.execute(
                "SELECT id, claim_type, text, status, metadata_json "
                "FROM claims ORDER BY id"
            ).fetchall()
            edges = db.execute(
                "SELECT claim_id, evidence_id, relation FROM claim_evidence ORDER BY claim_id, evidence_id"
            ).fetchall()
        by_claim: dict[str, list[dict[str, str]]] = {}
        for edge in edges:
            by_claim.setdefault(edge["claim_id"], []).append(
                {"evidence_id": edge["evidence_id"], "relation": edge["relation"]}
            )
        manifest_claims: list[dict[str, Any]] = []
        for row in claims:
            try:
                metadata = json.loads(row["metadata_json"])
            except (TypeError, json.JSONDecodeError):
                metadata = {}
            recorded_span = (
                metadata.get("tex_span", {})
                if isinstance(metadata, dict)
                else {}
            )
            manifest_claims.append(
                {
                    "claim_id": row["id"],
                    "claim_type": row["claim_type"],
                    "text": row["text"],
                    "status": row["status"],
                    "metadata": metadata,
                    "tex_span": dict(tex_spans.get(row["id"], recorded_span)),
                    "evidence": by_claim.get(row["id"], []),
                }
            )
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": utc_now(),
            "claims": manifest_claims,
        }
