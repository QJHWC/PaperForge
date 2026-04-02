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
from typing import Any, Dict, Iterable, Optional

from engine.research_partner.proposal_bundle import materialize_proposal_bundle
from engine.workspace_config import load_workspace_config, resolve_config_path


ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = (ROOT / "results").resolve()
PENDING_RUNS_DIR = RESULTS_DIR / ".frontend_runs_pending"
WORKSPACE_PATTERN = re.compile(r"\[(?:bootstrap|feedback|optimize|refine|cloud|done)\]\s+workspace=(.+)")


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def workspace_rel_to_abs(workspace_rel: str) -> Path:
    return (RESULTS_DIR / workspace_rel).resolve()


def workspace_abs_to_rel(workspace: Path) -> str:
    return workspace.resolve().relative_to(RESULTS_DIR).as_posix()


def _run_dir_for_workspace(workspace: Path) -> Path:
    return workspace / "artifacts" / "frontend_runs"


def _pending_run_dir() -> Path:
    PENDING_RUNS_DIR.mkdir(parents=True, exist_ok=True)
    return PENDING_RUNS_DIR


def _json_dump(path: Path, payload: Dict[str, Any]) -> None:
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
    details: Dict[str, Any] | None = None

    def to_payload(self) -> Dict[str, Any]:
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
        self.root = root
        self.results_dir = results_dir
        self._lock = threading.RLock()
        self._runs: Dict[str, ManagedRun] = {}

    def _next_run_id(self) -> str:
        return f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"

    def _workspace_rel_to_abs(self, workspace_rel: str) -> Path:
        return (self.results_dir / workspace_rel).resolve()

    def _workspace_abs_to_rel(self, workspace: Path) -> str:
        return workspace.resolve().relative_to(self.results_dir).as_posix()

    def _default_env(self, gateway_profile: str | None = None) -> Dict[str, str]:
        env = os.environ.copy()
        env.setdefault("PAPERFORGE_ALLOW_SYSTEM_PYTHON", "1")
        if gateway_profile:
            env["PAPERFORGE_GATEWAY_PROFILE"] = str(gateway_profile)
        return env

    def _resolve_workspace_config(self, workspace_rel: str | None) -> Dict[str, Any]:
        if not workspace_rel:
            return {}
        workspace = self._workspace_rel_to_abs(workspace_rel)
        if not workspace.exists():
            return {}
        return load_workspace_config(workspace)

    def _build_command(self, request: Dict[str, Any]) -> tuple[list[str], str | None, Dict[str, Any], Dict[str, str]]:
        entry = str(request["entry"])
        workspace_rel = request.get("workspace_rel")
        config = self._resolve_workspace_config(workspace_rel)
        env = self._default_env(str(request.get("gateway_profile") or config.get("gateway_profile") or "safe"))

        if entry == "mvp":
            phase = str(request.get("phase") or "refine")
            cmd = [sys.executable, str(self.root / "launch_mvp_workflow.py"), "--phase", phase]
            experiment = str(request.get("experiment") or (workspace_rel.split("/")[0] if workspace_rel else "paper_writer"))
            cmd.extend(["--experiment", experiment])
            if workspace_rel:
                cmd.extend(["--run-dir", str(self._workspace_rel_to_abs(workspace_rel))])
            if request.get("idea_name"):
                cmd.extend(["--idea-name", str(request["idea_name"])])
            if request.get("title"):
                cmd.extend(["--title", str(request["title"])])
            if request.get("description"):
                cmd.extend(["--description", str(request["description"])])
            cmd.extend(["--engine", str(request.get("engine") or "openalex")])
            writeup_model = str(request.get("writeup_model") or config.get("writeup_model") or "gpt-5.4-xhigh")
            cmd.extend(["--writeup-model", writeup_model])
            cmd.extend(["--refine-profile", str(request.get("refine_profile") or "balanced")])
            cmd.extend(["--gateway-profile", str(request.get("gateway_profile") or config.get("gateway_profile") or "safe")])
            existing_draft = request.get("existing_draft")
            if existing_draft is None and config.get("existing_draft"):
                existing_draft = resolve_config_path(self._workspace_rel_to_abs(workspace_rel), str(config["existing_draft"]))
            if existing_draft:
                cmd.extend(["--existing-draft", str(existing_draft)])
            enforce_disclosure = request.get("enforce_disclosure")
            if enforce_disclosure is None:
                enforce_disclosure = config.get("enforce_disclosure")
            if enforce_disclosure is True:
                cmd.append("--enforce-disclosure")
            if enforce_disclosure is False:
                cmd.append("--no-enforce-disclosure")
            skip_chktex_fix = request.get("skip_chktex_fix")
            if skip_chktex_fix is None:
                skip_chktex_fix = config.get("skip_chktex_fix")
            if skip_chktex_fix is True:
                cmd.append("--skip-chktex-fix")
            if skip_chktex_fix is False:
                cmd.append("--no-skip-chktex-fix")
            details = {
                "phase": phase,
                "writeup_model": writeup_model,
                "gateway_profile": env["PAPERFORGE_GATEWAY_PROFILE"],
            }
            return cmd, workspace_rel, details, env

        if entry == "scientist":
            cmd = [sys.executable, str(self.root / "launch_scientist.py")]
            experiment = str(request.get("experiment") or "paper_writer")
            cmd.extend(["--experiment", experiment])
            cmd.extend(["--engine", str(request.get("engine") or "openalex")])
            cmd.extend(["--num-ideas", str(request.get("num_ideas") or 1)])
            writeup_model = str(request.get("writeup_model") or "gpt-5.4-xhigh")
            model = str(request.get("model") or writeup_model)
            review_model = str(request.get("review_model") or model)
            cmd.extend(["--model", model, "--review-model", review_model])
            if request.get("idea_model"):
                cmd.extend(["--idea-model", str(request["idea_model"])])
            if request.get("code_model"):
                cmd.extend(["--code-model", str(request["code_model"])])
            if request.get("writeup_model"):
                cmd.extend(["--writeup-model", str(request["writeup_model"])])
            cmd.extend(["--gateway-profile", str(request.get("gateway_profile") or "safe")])
            details = {
                "experiment": experiment,
                "gateway_profile": env["PAPERFORGE_GATEWAY_PROFILE"],
            }
            return cmd, workspace_rel, details, env

        if entry == "writeup":
            workflow_kind = str(request.get("workflow_kind") or "mvp")
            target = request.get("workspace_rel") or request.get("workspace")
            if not target:
                raise ValueError("writeup requires workspace_rel or workspace")
            if workflow_kind == "scientist":
                folder = Path(str(target)).expanduser().resolve()
                cmd = [sys.executable, str(self.root / "engine" / "perform_writeup.py"), "--folder", str(folder)]
            else:
                workspace_rel = str(target)
                cmd = [
                    sys.executable,
                    str(self.root / "launch_mvp_workflow.py"),
                    "--phase",
                    "refine",
                    "--run-dir",
                    str(self._workspace_rel_to_abs(workspace_rel)),
                ]
            writeup_model = str(request.get("writeup_model") or config.get("writeup_model") or "gpt-5.4-xhigh")
            cmd.extend(["--writeup-model" if workflow_kind == "mvp" else "--model", writeup_model])
            cmd.extend(["--engine", str(request.get("engine") or "openalex")])
            if workflow_kind == "mvp":
                cmd.extend(["--refine-profile", str(request.get("refine_profile") or "balanced")])
                cmd.extend(["--gateway-profile", str(request.get("gateway_profile") or config.get("gateway_profile") or "safe")])
                existing_draft = request.get("existing_draft")
                if existing_draft is None and config.get("existing_draft") and workspace_rel:
                    existing_draft = resolve_config_path(self._workspace_rel_to_abs(workspace_rel), str(config["existing_draft"]))
                if existing_draft:
                    cmd.extend(["--existing-draft", str(existing_draft)])
                enforce_disclosure = request.get("enforce_disclosure")
                if enforce_disclosure is None:
                    enforce_disclosure = config.get("enforce_disclosure")
                if enforce_disclosure is True:
                    cmd.append("--enforce-disclosure")
                if enforce_disclosure is False:
                    cmd.append("--no-enforce-disclosure")
                skip_chktex_fix = request.get("skip_chktex_fix")
                if skip_chktex_fix is None:
                    skip_chktex_fix = config.get("skip_chktex_fix")
                if skip_chktex_fix is True:
                    cmd.append("--skip-chktex-fix")
                if skip_chktex_fix is False:
                    cmd.append("--no-skip-chktex-fix")
            if request.get("no_writing"):
                cmd.append("--no-writing")
            details = {
                "workflow_kind": workflow_kind,
                "gateway_profile": env["PAPERFORGE_GATEWAY_PROFILE"],
            }
            return cmd, workspace_rel if workflow_kind == "mvp" else None, details, env

        if entry == "research_partner":
            experiment = str(request.get("experiment") or "paper_writer")
            cmd = [sys.executable, str(self.root / "launch_mvp_workflow.py"), "--phase", "bootstrap", "--experiment", experiment]
            if workspace_rel:
                cmd.extend(["--run-dir", str(self._workspace_rel_to_abs(workspace_rel))])
            if request.get("idea_name"):
                cmd.extend(["--idea-name", str(request["idea_name"])])
            if request.get("title"):
                cmd.extend(["--title", str(request["title"])])
            if request.get("description"):
                cmd.extend(["--description", str(request["description"])])
            cmd.extend(["--engine", str(request.get("engine") or "openalex")])
            cmd.extend(["--literature-top-k", str(request.get("literature_top_k") or 5)])
            cmd.extend(["--rubric-profile", str(request.get("rubric_profile") or "default")])
            cmd.extend(["--skip-mvp-run", "--skip-writeup", "--refresh-literature"])
            details = {
                "title": str(request.get("title") or ""),
                "description": str(request.get("description") or ""),
                "rubric_profile": str(request.get("rubric_profile") or "default"),
                "evidence_files": list(request.get("evidence_files") or []),
            }
            return cmd, workspace_rel, details, env

        if entry == "migration":
            workspace_rel = str(request.get("workspace_rel") or "")
            if not workspace_rel:
                raise ValueError("migration requires workspace_rel")
            source_draft_id = str(request.get("source_draft_id") or "")
            template_id = str(request.get("template_id") or "")
            output_name = str(request.get("output_name") or "")
            if not source_draft_id or not template_id or not output_name:
                raise ValueError("migration requires source_draft_id, template_id, output_name")
            cmd = [
                sys.executable,
                str(self.root / "launch_template_migration.py"),
                "--workspace",
                str(self._workspace_rel_to_abs(workspace_rel)),
                "--source-draft-id",
                source_draft_id,
                "--template-id",
                template_id,
                "--output-name",
                output_name,
            ]
            details = {
                "source_draft_id": source_draft_id,
                "template_id": template_id,
                "output_name": output_name,
            }
            return cmd, workspace_rel, details, env

        raise ValueError(f"unsupported entry: {entry}")

    def launch_process(
        self,
        *,
        entry: str,
        command: list[str],
        workspace_rel: str | None,
        env: Dict[str, str] | None = None,
        details: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        run_id = self._next_run_id()
        record_dir = _run_dir_for_workspace(self._workspace_rel_to_abs(workspace_rel)) if workspace_rel else _pending_run_dir()
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
            pgid=os.getpgid(proc.pid) if proc.pid else None,
            details=details or {},
        )
        log_path.parent.mkdir(parents=True, exist_ok=True)
        _json_dump(meta_path, managed.to_payload())
        with self._lock:
            self._runs[run_id] = managed
        thread = threading.Thread(target=self._drain_process_output, args=(managed,), daemon=True)
        thread.start()
        return managed.to_payload()

    def start_run(self, request: Dict[str, Any]) -> Dict[str, Any]:
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
                log.write(line)
                log.flush()
                self._maybe_capture_workspace(managed, line)
            returncode = int(proc.wait())
        if managed.entry == "research_partner" and returncode == 0 and managed.workspace_rel:
            try:
                materialize_proposal_bundle(
                    workspace=self._workspace_rel_to_abs(managed.workspace_rel),
                    title=str(managed.details.get("title") or "Evidence-first Research Partner Session"),
                    description=str(managed.details.get("description") or ""),
                    rubric_profile=str(managed.details.get("rubric_profile") or "default"),
                    evidence_files=list(managed.details.get("evidence_files") or []),
                )
            except Exception as exc:
                with managed.log_path.open("a", encoding="utf-8") as log:
                    log.write(f"\n[research_partner][materialize] failed: {exc}\n")
                returncode = 1
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
        if not str(workspace).startswith(str(self.results_dir)):
            return
        workspace_rel = self._workspace_abs_to_rel(workspace)
        if workspace_rel == managed.workspace_rel:
            return
        new_dir = _run_dir_for_workspace(workspace)
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

    def pause_run(self, run_id: str) -> Dict[str, Any]:
        managed = self._active_run(run_id)
        if os.name != "posix":
            raise RuntimeError("pause/resume is only supported on POSIX")
        assert managed.pgid is not None
        os.killpg(managed.pgid, signal.SIGSTOP)
        managed.status = "paused"
        _json_dump(managed.meta_path, managed.to_payload())
        return managed.to_payload()

    def resume_run(self, run_id: str) -> Dict[str, Any]:
        managed = self._active_run(run_id)
        if os.name != "posix":
            raise RuntimeError("pause/resume is only supported on POSIX")
        assert managed.pgid is not None
        os.killpg(managed.pgid, signal.SIGCONT)
        managed.status = "running"
        _json_dump(managed.meta_path, managed.to_payload())
        return managed.to_payload()

    def stop_run(self, run_id: str) -> Dict[str, Any]:
        managed = self._active_run(run_id)
        assert managed.pgid is not None
        managed.status = "stopping"
        _json_dump(managed.meta_path, managed.to_payload())
        os.killpg(managed.pgid, signal.SIGTERM)
        try:
            managed.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(managed.pgid, signal.SIGKILL)
        return managed.to_payload()

    def list_run_records(self, workspace_rel: str) -> list[Dict[str, Any]]:
        workspace = self._workspace_rel_to_abs(workspace_rel)
        run_dir = _run_dir_for_workspace(workspace)
        records: list[Dict[str, Any]] = []
        if run_dir.exists():
            for path in sorted(run_dir.glob("*.json"), reverse=True):
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

    def read_log(self, workspace_rel: str, run_id: str, offset: int = 0) -> Dict[str, Any]:
        records = self.list_run_records(workspace_rel)
        record = next((item for item in records if item.get("run_id") == run_id), None)
        if record is None:
            raise FileNotFoundError(run_id)
        log_path = Path(str(record["log_path"]))
        if not log_path.exists():
            return {"run_id": run_id, "offset": offset, "next_offset": offset, "text": "", "status": record.get("status")}
        with log_path.open("rb") as f:
            f.seek(max(0, int(offset)))
            chunk = f.read()
            next_offset = f.tell()
        return {
            "run_id": run_id,
            "offset": offset,
            "next_offset": next_offset,
            "text": chunk.decode("utf-8", errors="replace"),
            "status": record.get("status"),
        }
