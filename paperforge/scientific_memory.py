from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .models import ClaimRelation, ClaimStatus, ClaimType, utc_now

SCHEMA_VERSION = 1
PUBLICATION_STATUSES = {
    ClaimStatus.SUPPORTED_STATIC.value,
    ClaimStatus.VERIFIED_RUNTIME.value,
    ClaimStatus.VERIFIED_EXPERIMENT.value,
}


def _json(value: Mapping[str, Any] | Sequence[Any] | None) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True)


def _stable_id(prefix: str, *parts: object) -> str:
    digest = hashlib.sha256("\x1f".join(str(part) for part in parts).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:24]}"


class ScientificMemory:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
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
                    _json(metadata),
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
                SELECT c.id, c.claim_type, c.text, c.status,
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
        for row in rows:
            status = str(row["status"])
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
        return {
            "passed": bool(rows) and not failures,
            "claim_count": len(rows),
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
                    "tex_span": dict(tex_spans.get(row["id"], recorded_span)),
                    "evidence": by_claim.get(row["id"], []),
                }
            )
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": utc_now(),
            "claims": manifest_claims,
        }
