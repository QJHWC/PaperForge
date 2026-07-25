from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .models import CompletionGate, ExecutionProfile, WorkflowStatus, utc_now
from .scientific_memory import ScientificMemory, _stable_id


class InvalidTransition(RuntimeError):
    pass


_TRANSITIONS = {
    WorkflowStatus.READY: {
        WorkflowStatus.RUNNING,
        WorkflowStatus.AUTH_BLOCKED,
        WorkflowStatus.FAILED,
        WorkflowStatus.CANCELLED,
    },
    WorkflowStatus.RUNNING: {
        WorkflowStatus.PAUSED,
        WorkflowStatus.INTERRUPTED,
        WorkflowStatus.AWAITING_APPROVAL,
        WorkflowStatus.AUTH_BLOCKED,
        WorkflowStatus.FAILED,
        WorkflowStatus.CANCELLED,
        WorkflowStatus.COMPLETED,
    },
    WorkflowStatus.PAUSED: {
        WorkflowStatus.RUNNING,
        WorkflowStatus.CANCELLED,
        WorkflowStatus.FAILED,
    },
    WorkflowStatus.INTERRUPTED: {
        WorkflowStatus.RUNNING,
        WorkflowStatus.CANCELLED,
        WorkflowStatus.FAILED,
    },
    WorkflowStatus.AWAITING_APPROVAL: {
        WorkflowStatus.RUNNING,
        WorkflowStatus.FAILED,
        WorkflowStatus.CANCELLED,
    },
    WorkflowStatus.AUTH_BLOCKED: {
        WorkflowStatus.READY,
        WorkflowStatus.RUNNING,
        WorkflowStatus.FAILED,
        WorkflowStatus.CANCELLED,
    },
    WorkflowStatus.FAILED: {
        WorkflowStatus.READY,
        WorkflowStatus.RUNNING,
        WorkflowStatus.CANCELLED,
    },
    WorkflowStatus.CANCELLED: set(),
    WorkflowStatus.COMPLETED: set(),
}


class WorkflowEngine:
    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.state_dir = self.workspace / ".paperforge"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.memory = ScientificMemory(self.state_dir / "paperforge.db")

    def create(
        self,
        profile: ExecutionProfile | str,
        *,
        metadata: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        normalized_profile = ExecutionProfile(profile)
        workflow_id = (
            _stable_id("wf", str(self.workspace), normalized_profile.value, idempotency_key)
            if idempotency_key
            else f"wf_{uuid.uuid4().hex[:24]}"
        )
        now = utc_now()
        with self.memory.connect() as db:
            db.execute(
                """
                INSERT INTO workflows
                (id, profile, status, workspace, checkpoint, started_at, updated_at,
                 error_code, version, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 0, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (
                    workflow_id,
                    normalized_profile.value,
                    WorkflowStatus.READY.value,
                    str(self.workspace),
                    "created",
                    now,
                    now,
                    json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
                ),
            )
        return workflow_id

    def get(self, workflow_id: str) -> dict[str, Any]:
        with self.memory.connect() as db:
            row = db.execute("SELECT * FROM workflows WHERE id = ?", (workflow_id,)).fetchone()
        if row is None:
            raise KeyError(workflow_id)
        payload = dict(row)
        payload["metadata"] = json.loads(payload.pop("metadata_json"))
        return payload

    def latest(self) -> dict[str, Any]:
        with self.memory.connect() as db:
            row = db.execute(
                "SELECT id FROM workflows ORDER BY started_at DESC, rowid DESC LIMIT 1"
            ).fetchone()
        if row is None:
            raise KeyError("no workflow exists")
        return self.get(str(row["id"]))

    def transition(
        self,
        workflow_id: str,
        status: WorkflowStatus | str,
        *,
        checkpoint: str | None = None,
        error_code: str | None = None,
        gate: CompletionGate | None = None,
        detail: str = "",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        target = WorkflowStatus(status)
        if target is WorkflowStatus.COMPLETED and (gate is None or not gate.passed):
            raise InvalidTransition("COMPLETED requires every release gate to pass")
        now = utc_now()
        with self.memory.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status, version FROM workflows WHERE id = ?",
                (workflow_id,),
            ).fetchone()
            if row is None:
                raise KeyError(workflow_id)
            current = WorkflowStatus(row["status"])
            if target is current:
                db.commit()
                return self.get(workflow_id)
            if target not in _TRANSITIONS[current]:
                raise InvalidTransition(f"{current.value} -> {target.value} is not allowed")
            updated = db.execute(
                """
                UPDATE workflows
                SET status = ?, checkpoint = COALESCE(?, checkpoint), updated_at = ?,
                    error_code = ?, version = version + 1
                WHERE id = ? AND version = ?
                """,
                (
                    target.value,
                    checkpoint,
                    now,
                    error_code,
                    workflow_id,
                    int(row["version"]),
                ),
            )
            if updated.rowcount != 1:
                raise InvalidTransition("workflow changed concurrently; retry from latest state")
            db.execute(
                """
                INSERT OR IGNORE INTO workflow_events
                (workflow_id, stage, status, detail, idempotency_key, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    workflow_id,
                    checkpoint or "workflow",
                    target.value,
                    detail,
                    idempotency_key,
                    now,
                ),
            )
        return self.get(workflow_id)

    def approve(
        self,
        proposal_id: str,
        *,
        approved_by: str,
        scope: Mapping[str, Any] | None = None,
    ) -> str:
        approval_id = _stable_id("approval", proposal_id, approved_by, json.dumps(scope or {}, sort_keys=True))
        with self.memory.connect() as db:
            db.execute(
                """
                INSERT OR REPLACE INTO approvals
                (id, proposal_id, status, approved_by, approved_at, scope_json)
                VALUES (?, ?, 'APPROVED', ?, ?, ?)
                """,
                (
                    approval_id,
                    proposal_id,
                    approved_by,
                    utc_now(),
                    json.dumps(scope or {}, ensure_ascii=False, sort_keys=True),
                ),
            )
        return approval_id

    def record_checkpoint(
        self,
        workflow_id: str,
        *,
        checkpoint: str,
        detail: str,
        metadata_updates: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.memory.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT version, metadata_json FROM workflows WHERE id = ?",
                (workflow_id,),
            ).fetchone()
            if row is None:
                raise KeyError(workflow_id)
            metadata = json.loads(row["metadata_json"])
            metadata.update(dict(metadata_updates or {}))
            updated = db.execute(
                """
                UPDATE workflows
                SET checkpoint = ?, updated_at = ?, version = version + 1,
                    metadata_json = ?
                WHERE id = ? AND version = ?
                """,
                (
                    checkpoint,
                    now,
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                    workflow_id,
                    int(row["version"]),
                ),
            )
            if updated.rowcount != 1:
                raise InvalidTransition(
                    "workflow changed concurrently; retry from latest state"
                )
            db.execute(
                """
                INSERT OR IGNORE INTO workflow_events
                (workflow_id, stage, status, detail, idempotency_key, created_at)
                SELECT id, ?, status, ?, ?, ? FROM workflows WHERE id = ?
                """,
                (
                    checkpoint,
                    detail,
                    idempotency_key,
                    now,
                    workflow_id,
                ),
            )
        return self.get(workflow_id)

    def require_approval(self, proposal_id: str) -> dict[str, Any]:
        with self.memory.connect() as db:
            row = db.execute(
                """
                SELECT * FROM approvals
                WHERE proposal_id = ? AND status = 'APPROVED'
                ORDER BY approved_at DESC LIMIT 1
                """,
                (proposal_id,),
            ).fetchone()
        if row is None:
            raise PermissionError(f"proposal is not approved: {proposal_id}")
        payload = dict(row)
        payload["scope"] = json.loads(payload.pop("scope_json"))
        return payload
