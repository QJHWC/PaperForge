from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from engine.secret_redaction import redact_secrets, redact_structure

from .models import CompletionGate, ExecutionProfile, WorkflowStatus, utc_now
from .path_safety import safe_mkdir
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
        safe_mkdir(self.state_dir, anchor=self.workspace)
        self.memory = ScientificMemory(
            self.state_dir / "paperforge.db",
            trusted_root=self.workspace,
        )

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
        safe_metadata = redact_structure(dict(metadata or {}))
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
                    json.dumps(safe_metadata, ensure_ascii=False, sort_keys=True),
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
        safe_detail = redact_secrets(detail)
        safe_error_code = (
            redact_secrets(error_code) if error_code is not None else None
        )
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
                    safe_error_code,
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
                    safe_detail,
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
        from .experiments import ExperimentManager

        return ExperimentManager(
            self.workspace,
            profile=ExecutionProfile.FULL,
            memory=self.memory,
        ).approve(
            proposal_id,
            approved_by=approved_by,
            scope=scope,
        )

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
        safe_detail = redact_secrets(detail)
        with self.memory.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT version, metadata_json FROM workflows WHERE id = ?",
                (workflow_id,),
            ).fetchone()
            if row is None:
                raise KeyError(workflow_id)
            metadata = json.loads(row["metadata_json"])
            metadata.update(redact_structure(dict(metadata_updates or {})))
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
                    safe_detail,
                    idempotency_key,
                    now,
                    workflow_id,
                ),
            )
        return self.get(workflow_id)

    def claim_runtime_execution(self, workflow_id: str) -> bool:
        """Atomically reserve one runtime execution for a workflow."""

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
            if metadata.get("runtime_executed") or metadata.get("runtime_claimed"):
                return False
            metadata["runtime_claimed"] = True
            metadata["runtime_claimed_at"] = now
            updated = db.execute(
                """
                UPDATE workflows
                SET updated_at = ?, version = version + 1, metadata_json = ?
                WHERE id = ? AND version = ?
                """,
                (
                    now,
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                    workflow_id,
                    int(row["version"]),
                ),
            )
            if updated.rowcount != 1:
                raise InvalidTransition(
                    "workflow changed concurrently; runtime was not claimed"
                )
        return True

    def release_runtime_claim(self, workflow_id: str) -> None:
        with self.memory.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT metadata_json FROM workflows WHERE id = ?",
                (workflow_id,),
            ).fetchone()
            if row is None:
                raise KeyError(workflow_id)
            metadata = json.loads(row["metadata_json"])
            metadata["runtime_claimed"] = False
            db.execute(
                """
                UPDATE workflows
                SET updated_at = ?, version = version + 1, metadata_json = ?
                WHERE id = ?
                """,
                (
                    utc_now(),
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                    workflow_id,
                ),
            )

    def require_approval(self, proposal_id: str) -> dict[str, Any]:
        with self.memory.connect() as db:
            proposal = db.execute(
                "SELECT id FROM experiments WHERE id = ?",
                (proposal_id,),
            ).fetchone()
            if proposal is None:
                raise PermissionError(
                    f"proposal is not approved: {proposal_id}"
                )
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
