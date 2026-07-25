from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .contracts import JobResult, JobSpec, utc_now


class ComputeStateStore:
    """Durable, secret-redacted compute state shared by all backends."""

    def __init__(self, state_dir: str | Path) -> None:
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.path = self.state_dir / "compute-state.sqlite3"

    def _connect(self) -> sqlite3.Connection:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS compute_jobs (
                backend TEXT NOT NULL,
                job_id TEXT NOT NULL,
                spec_json TEXT NOT NULL,
                result_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (backend, job_id)
            );
            """
        )
        return connection

    def save(self, backend: str, job_id: str, spec: JobSpec, result: JobResult) -> None:
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO compute_jobs
                (backend, job_id, spec_json, result_json, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(backend, job_id) DO UPDATE SET
                    spec_json=excluded.spec_json,
                    result_json=excluded.result_json,
                    updated_at=excluded.updated_at
                """,
                (
                    backend,
                    job_id,
                    json.dumps(spec.to_dict(), ensure_ascii=False, sort_keys=True),
                    json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True),
                    utc_now(),
                ),
            )

    def load_spec(self, backend: str, job_id: str) -> JobSpec | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT spec_json FROM compute_jobs WHERE backend = ? AND job_id = ?",
                (backend, job_id),
            ).fetchone()
        return JobSpec.from_dict(json.loads(row["spec_json"])) if row else None

    def load_result(self, backend: str, job_id: str) -> JobResult | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT result_json FROM compute_jobs WHERE backend = ? AND job_id = ?",
                (backend, job_id),
            ).fetchone()
        return JobResult.from_dict(json.loads(row["result_json"])) if row else None

