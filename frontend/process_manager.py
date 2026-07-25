from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from engine.secret_redaction import redact_secrets
from engine.workspace_config import load_workspace_config

ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = (ROOT / "results").resolve()
WORKSPACE_PATTERN = re.compile(r"\[(?:bootstrap|feedback|optimize|refine|cloud|done)\]\s+workspace=(.+)")
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
EXECUTION_PROFILES = frozenset({"writing-only", "research", "full"})
LEGACY_MODES = frozenset({"writeup", "research_partner", "mvp", "scientist"})
PUBLISH_TEMPLATES = frozenset({"generic", "cvpr", "ieee", "elsevier"})
GATEWAY_PROFILES = frozenset({"safe", "full"})
FORBIDDEN_BROWSER_KEYS = frozenset(
    {
        "args",
        "argv",
        "command",
        "cwd",
        "env",
        "environment",
        "executable",
        "python",
        "shell",
    }
)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _contained_path(root: Path, path: str | Path, *, allow_root: bool = False) -> Path:
    try:
        canonical_root = root.expanduser().resolve()
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = canonical_root / candidate
        candidate = candidate.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise PermissionError("invalid path") from exc
    try:
        relative = candidate.relative_to(canonical_root)
    except ValueError as exc:
        raise PermissionError("path escapes the configured results directory") from exc
    if not allow_root and not relative.parts:
        raise PermissionError("a workspace must be below the configured results directory")
    return candidate


def workspace_rel_to_abs(workspace_rel: str) -> Path:
    return _contained_path(RESULTS_DIR, workspace_rel)


def workspace_abs_to_rel(workspace: Path) -> str:
    return _contained_path(RESULTS_DIR, workspace).relative_to(RESULTS_DIR).as_posix()


def _json_dump(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


@dataclass
class ManagedRun:
    run_id: str
    entry: str
    workspace_rel: str | None
    command: list[str]
    status: str
    started_at: str
    ended_at: str | None
    log_path: Path
    meta_path: Path
    process: subprocess.Popen[str] | None
    exit_code: int | None = None
    pid: int | None = None
    pgid: int | None = None
    details: dict[str, Any] | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "entry": self.entry,
            "workspace_rel": self.workspace_rel,
            "command": self.command,
            "pid": self.pid,
            "pgid": self.pgid,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "log_path": str(self.log_path),
            "exit_code": self.exit_code,
            "details": dict(self.details or {}),
        }


class ProcessManager:
    def __init__(self, *, root: Path = ROOT, results_dir: Path = RESULTS_DIR) -> None:
        self.root = root.expanduser().resolve()
        self.results_dir = results_dir.expanduser().resolve()
        self._lock = threading.RLock()
        self._runs: dict[str, ManagedRun] = {}

    def _next_run_id(self) -> str:
        return f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"

    def _workspace_rel_to_abs(self, workspace_rel: str) -> Path:
        if not isinstance(workspace_rel, str) or not workspace_rel.strip() or "\x00" in workspace_rel:
            raise ValueError("workspace_rel must be a non-empty relative workspace path")
        try:
            return _contained_path(self.results_dir, workspace_rel.strip())
        except PermissionError as exc:
            raise ValueError("workspace_rel escapes the results directory") from exc

    def _workspace_abs_to_rel(self, workspace: Path) -> str:
        try:
            return _contained_path(self.results_dir, workspace).relative_to(self.results_dir).as_posix()
        except PermissionError as exc:
            raise ValueError("workspace escapes the results directory") from exc

    def _workspace_for_request(self, request: dict[str, Any]) -> tuple[str, Path]:
        raw = request.get("workspace_rel")
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("workspace_rel is required")
        workspace = self._workspace_rel_to_abs(raw)
        if not workspace.is_dir():
            raise ValueError(f"workspace not found: {raw}")
        return self._workspace_abs_to_rel(workspace), workspace

    def _run_record_dir(self, workspace: Path) -> Path:
        try:
            run_dir = _contained_path(workspace, Path("artifacts") / "frontend_runs")
            _contained_path(self.results_dir, run_dir)
        except PermissionError as exc:
            raise ValueError("frontend run directory escapes the workspace") from exc
        return run_dir

    def _pending_record_dir(self) -> Path:
        try:
            return _contained_path(self.results_dir, ".frontend_runs_pending")
        except PermissionError as exc:
            raise ValueError("pending run directory escapes the results directory") from exc

    def _default_env(self, gateway_profile: str | None = None) -> dict[str, str]:
        env = os.environ.copy()
        env.setdefault("PAPERFORGE_ALLOW_SYSTEM_PYTHON", "1")
        if gateway_profile:
            env["PAPERFORGE_GATEWAY_PROFILE"] = str(gateway_profile)
        return env

    def _resolve_workspace_config(self, workspace_rel: str | None) -> dict[str, Any]:
        if not workspace_rel:
            return {}
        workspace = self._workspace_rel_to_abs(workspace_rel)
        if not workspace.exists():
            return {}
        try:
            _contained_path(workspace, "workspace_config.json")
        except PermissionError as exc:
            raise ValueError("workspace config escapes the workspace") from exc
        return load_workspace_config(workspace)

    @staticmethod
    def _enum(value: Any, allowed: frozenset[str], label: str, default: str | None = None) -> str:
        normalized = str(value if value is not None else default or "").strip().lower()
        if normalized not in allowed:
            raise ValueError(f"unsupported {label}: {normalized or '<empty>'}")
        return normalized

    @staticmethod
    def _identifier(value: Any, label: str, *, required: bool = True) -> str | None:
        normalized = str(value or "").strip()
        if not normalized:
            if required:
                raise ValueError(f"{label} is required")
            return None
        if not IDENTIFIER_PATTERN.fullmatch(normalized):
            raise ValueError(f"invalid {label}")
        return normalized

    def _build_command(self, request: dict[str, Any]) -> tuple[list[str], str | None, dict[str, Any], dict[str, str]]:
        forbidden = sorted(FORBIDDEN_BROWSER_KEYS.intersection(request))
        if forbidden:
            raise ValueError(f"browser command overrides are forbidden: {', '.join(forbidden)}")

        entry = str(request["entry"]).strip().lower()
        command = [sys.executable, "-m", "paperforge"]
        details: dict[str, Any] = {"cli_action": entry}
        workspace_rel: str | None = None

        legacy_profiles = {
            "writeup": "writing-only",
            "research_partner": "research",
            "mvp": "full",
            "scientist": "full",
        }
        if entry in legacy_profiles:
            workspace_rel, workspace = self._workspace_for_request(request)
            profile = self._enum(request.get("profile"), EXECUTION_PROFILES, "profile", legacy_profiles[entry])
            command.extend(
                [
                    "run",
                    "--profile",
                    profile,
                    "--workspace",
                    str(workspace),
                    "--legacy-mode",
                    entry,
                ]
            )
            details.update({"cli_action": "run", "profile": profile, "legacy_mode": entry})
        elif entry == "run":
            workspace_rel, workspace = self._workspace_for_request(request)
            profile = self._enum(request.get("profile"), EXECUTION_PROFILES, "profile", "full")
            command.extend(["run", "--profile", profile, "--workspace", str(workspace)])
            legacy_mode = request.get("legacy_mode")
            if legacy_mode is not None:
                legacy_mode = self._enum(legacy_mode, LEGACY_MODES, "legacy_mode")
                command.extend(["--legacy-mode", legacy_mode])
            details.update({"profile": profile, "legacy_mode": legacy_mode})
        elif entry == "preflight":
            if request.get("workspace_rel"):
                workspace_rel, workspace = self._workspace_for_request(request)
            command.append("preflight")
            if workspace_rel:
                command.extend(["--workspace", str(workspace)])
            live_provider = request.get("live_provider", False)
            if not isinstance(live_provider, bool):
                raise ValueError("live_provider must be a boolean")
            if live_provider:
                command.append("--live-provider")
            details["live_provider"] = live_provider
        elif entry == "approve":
            workspace_rel, workspace = self._workspace_for_request(request)
            proposal_id = self._identifier(request.get("proposal_id"), "proposal_id")
            command.extend(["approve", "--proposal-id", proposal_id, "--workspace", str(workspace)])
            details["proposal_id"] = proposal_id
        elif entry == "resume":
            workspace_rel, workspace = self._workspace_for_request(request)
            command.extend(["resume", "--workspace", str(workspace)])
            run_id = self._identifier(request.get("target_run_id") or request.get("run_id"), "run_id", required=False)
            if run_id:
                command.extend(["--run-id", run_id])
            details["target_run_id"] = run_id
        elif entry in {"publish", "migration"}:
            workspace_rel, workspace = self._workspace_for_request(request)
            template = self._enum(request.get("template"), PUBLISH_TEMPLATES, "template", "generic")
            command.extend(["publish", "--template", template, "--workspace", str(workspace)])
            details.update(
                {
                    "cli_action": "publish",
                    "template": template,
                    "legacy_migration": entry == "migration",
                }
            )
        elif entry in {"release", "status"}:
            workspace_rel, workspace = self._workspace_for_request(request)
            command.extend([entry, "--workspace", str(workspace)])
            if entry == "status":
                run_id = self._identifier(request.get("target_run_id") or request.get("run_id"), "run_id", required=False)
                if run_id:
                    command.extend(["--run-id", run_id])
                details["target_run_id"] = run_id
        else:
            raise ValueError(f"unsupported entry: {entry}")

        config = self._resolve_workspace_config(workspace_rel)
        gateway_profile = request.get("gateway_profile") or config.get("gateway_profile") or "safe"
        gateway_profile = self._enum(gateway_profile, GATEWAY_PROFILES, "gateway_profile")
        env = self._default_env(gateway_profile)
        details["gateway_profile"] = gateway_profile
        return command, workspace_rel, details, env

    def launch_process(
        self,
        *,
        entry: str,
        command: list[str],
        workspace_rel: str | None,
        env: dict[str, str] | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        run_id = self._next_run_id()
        record_dir = (
            self._run_record_dir(self._workspace_rel_to_abs(workspace_rel))
            if workspace_rel
            else self._pending_record_dir()
        )
        log_path = record_dir / f"{run_id}.log"
        meta_path = record_dir / f"{run_id}.json"
        proc = subprocess.Popen(
            command,
            cwd=str(self.root),
            env=env or self._default_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            start_new_session=True,
        )
        managed = ManagedRun(
            run_id=run_id,
            entry=entry,
            workspace_rel=workspace_rel,
            command=list(command),
            status="running",
            started_at=now_iso(),
            ended_at=None,
            log_path=log_path,
            meta_path=meta_path,
            process=proc,
            exit_code=None,
            pid=proc.pid,
            pgid=(os.getpgid(proc.pid) if os.name == "posix" and proc.pid else proc.pid),
            details=details or {},
        )
        log_path.parent.mkdir(parents=True, exist_ok=True)
        _json_dump(meta_path, managed.to_payload())
        with self._lock:
            self._runs[run_id] = managed
        thread = threading.Thread(target=self._drain_process_output, args=(managed,), daemon=True)
        thread.start()
        return managed.to_payload()

    def start_run(self, request: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(request, dict):
            raise ValueError("run request must be an object")
        if "entry" not in request:
            raise ValueError("entry is required")
        command, workspace_rel, details, env = self._build_command(request)
        return self.launch_process(
            entry=str(request["entry"]),
            command=command,
            workspace_rel=workspace_rel,
            env=env,
            details=details,
        )

    def _drain_process_output(self, managed: ManagedRun) -> None:
        proc = managed.process
        assert proc is not None
        managed.log_path.parent.mkdir(parents=True, exist_ok=True)
        with managed.log_path.open("a", encoding="utf-8") as log:
            for line in proc.stdout or []:
                log.write(redact_secrets(line))
                log.flush()
                self._maybe_capture_workspace(managed, line)
            returncode = int(proc.wait())
        with self._lock:
            managed.exit_code = returncode
            managed.ended_at = now_iso()
            managed.status = "completed" if returncode == 0 else "failed"
            _json_dump(managed.meta_path, managed.to_payload())
            self._runs.pop(managed.run_id, None)

    def _maybe_capture_workspace(self, managed: ManagedRun, line: str) -> None:
        match = WORKSPACE_PATTERN.search(str(line))
        if not match:
            return
        workspace_text = match.group(1).strip()
        workspace = Path(workspace_text).expanduser()
        if not workspace.is_absolute():
            workspace = (self.root / workspace).resolve()
        try:
            workspace = _contained_path(self.results_dir, workspace)
        except PermissionError:
            return
        workspace_rel = self._workspace_abs_to_rel(workspace)
        if workspace_rel == managed.workspace_rel:
            return
        new_dir = self._run_record_dir(workspace)
        new_dir.mkdir(parents=True, exist_ok=True)
        new_log_path = new_dir / managed.log_path.name
        new_meta_path = new_dir / managed.meta_path.name
        if managed.log_path.exists():
            shutil.move(str(managed.log_path), str(new_log_path))
        if managed.meta_path.exists():
            shutil.move(str(managed.meta_path), str(new_meta_path))
        managed.workspace_rel = workspace_rel
        managed.log_path = new_log_path
        managed.meta_path = new_meta_path
        _json_dump(managed.meta_path, managed.to_payload())

    def _active_run(self, run_id: str) -> ManagedRun:
        with self._lock:
            managed = self._runs.get(run_id)
        if managed is None or managed.process is None:
            raise KeyError(run_id)
        return managed

    def pause_run(self, run_id: str) -> dict[str, Any]:
        managed = self._active_run(run_id)
        if os.name != "posix":
            raise RuntimeError("pause/resume is only supported on POSIX")
        assert managed.pgid is not None
        os.killpg(managed.pgid, signal.SIGSTOP)
        managed.status = "paused"
        _json_dump(managed.meta_path, managed.to_payload())
        return managed.to_payload()

    def resume_run(self, run_id: str) -> dict[str, Any]:
        managed = self._active_run(run_id)
        if os.name != "posix":
            raise RuntimeError("pause/resume is only supported on POSIX")
        assert managed.pgid is not None
        os.killpg(managed.pgid, signal.SIGCONT)
        managed.status = "running"
        _json_dump(managed.meta_path, managed.to_payload())
        return managed.to_payload()

    def stop_run(self, run_id: str) -> dict[str, Any]:
        managed = self._active_run(run_id)
        assert managed.pgid is not None
        managed.status = "stopping"
        _json_dump(managed.meta_path, managed.to_payload())
        if os.name == "posix":
            os.killpg(managed.pgid, signal.SIGTERM)
        else:
            managed.process.terminate()
        try:
            managed.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                os.killpg(managed.pgid, signal.SIGKILL)
            else:
                managed.process.kill()
        return managed.to_payload()

    def list_run_records(self, workspace_rel: str) -> list[dict[str, Any]]:
        workspace = self._workspace_rel_to_abs(workspace_rel)
        run_dir = self._run_record_dir(workspace)
        records: list[dict[str, Any]] = []
        if run_dir.exists():
            for path in sorted(run_dir.glob("*.json"), reverse=True):
                try:
                    _contained_path(run_dir, path)
                except PermissionError:
                    continue
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                records.append(payload)
        with self._lock:
            for managed in self._runs.values():
                if managed.workspace_rel == workspace_rel:
                    active_payload = managed.to_payload()
                    records = [r for r in records if r.get("run_id") != managed.run_id]
                    records.insert(0, active_payload)
        records.sort(key=lambda item: item.get("started_at", ""), reverse=True)
        return records

    def read_log(self, workspace_rel: str, run_id: str, offset: int = 0) -> dict[str, Any]:
        normalized_run_id = self._identifier(run_id, "run_id")
        assert normalized_run_id is not None
        records = self.list_run_records(workspace_rel)
        record = next((item for item in records if item.get("run_id") == normalized_run_id), None)
        if record is None:
            raise FileNotFoundError(normalized_run_id)
        workspace = self._workspace_rel_to_abs(workspace_rel)
        run_dir = self._run_record_dir(workspace)
        try:
            log_path = _contained_path(run_dir, f"{normalized_run_id}.log")
        except PermissionError as exc:
            raise FileNotFoundError(normalized_run_id) from exc
        if not log_path.exists():
            return {
                "run_id": normalized_run_id,
                "offset": offset,
                "next_offset": offset,
                "text": "",
                "status": record.get("status"),
            }
        with log_path.open("rb") as f:
            f.seek(max(0, int(offset)))
            chunk = f.read()
            next_offset = f.tell()
        return {
            "run_id": normalized_run_id,
            "offset": offset,
            "next_offset": next_offset,
            "text": chunk.decode("utf-8", errors="replace"),
            "status": record.get("status"),
        }
