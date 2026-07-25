"""PaperForge 前端 HTTP 服务器。

提供静态文件服务 + JSON API，供浏览器前端查询 workspace、控制流程、读取日志与打开产物。
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sqlite3
import sys
from email import policy as email_policy
from email.parser import BytesParser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = Path(__file__).resolve().parent
RESULTS_DIR = (ROOT / "results").resolve()

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import re as _re

from engine.secret_redaction import redact_secrets
from engine.template_migration import (
    import_source_draft,
    import_template_directory,
    recycle_workspace_artifact,
    restore_recycled_artifact,
)
from engine.workspace_config import save_workspace_config
from frontend.process_manager import ProcessManager
from paperforge.provider import ProviderRegistry

PROCESS_MANAGER = ProcessManager(root=ROOT, results_dir=RESULTS_DIR)
_RUN_DIR_PATTERN = _re.compile(r"^run_(\d+)$")
_IDENTIFIER_PATTERN = _re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_MODEL_PATTERN = _re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
_STATE_SUMMARY_KEYS = ["phase", "current_phase", "completed_phases", "status", "idea_name", "created_at", "updated_at"]
_CONFIG_KEYS = {
    "writeup_model",
    "gateway_profile",
    "existing_draft",
    "skip_chktex_fix",
    "active_source_draft_id",
    "active_template_id",
    "latest_migration_id",
}
_LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_SESSION_COOKIE = "PaperForge-Session"
_CSRF_HEADER = "X-PaperForge-CSRF"
_MAX_JSON_BODY = 1024 * 1024
_MAX_UPLOAD_BODY = 64 * 1024 * 1024
_MAX_UPLOAD_FILES = 256
_PREVIEW_SUFFIXES = frozenset(
    {
        ".bib",
        ".csv",
        ".html",
        ".jpeg",
        ".jpg",
        ".json",
        ".log",
        ".md",
        ".pdf",
        ".png",
        ".svg",
        ".tex",
        ".txt",
    }
)
_SUPPORTED_CLAIM_STATUSES = frozenset(
    {"SUPPORTED_STATIC", "VERIFIED_RUNTIME", "VERIFIED_EXPERIMENT"}
)


class RequestError(ValueError):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _contained_path(root: Path, value: str | Path, *, allow_root: bool = False) -> Path:
    try:
        canonical_root = root.expanduser().resolve()
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = canonical_root / candidate
        candidate = candidate.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise PermissionError("forbidden") from exc
    try:
        relative = candidate.relative_to(canonical_root)
    except ValueError as exc:
        raise PermissionError("forbidden") from exc
    if not allow_root and not relative.parts:
        raise PermissionError("forbidden")
    return candidate


def _safe_workspace_child(workspace: Path, value: str | Path, *, allow_root: bool = False) -> Path:
    return _contained_path(workspace, value, allow_root=allow_root)


def _ensure_workspace_targets(workspace: Path, names: list[str]) -> None:
    for name in names:
        _safe_workspace_child(workspace, name)


def _valid_identifier(value: object, label: str) -> str:
    normalized = str(value or "").strip()
    if not _IDENTIFIER_PATTERN.fullmatch(normalized):
        raise RequestError(f"invalid {label}")
    return normalized


def _read_small_json(path: Path, root: Path, *, max_bytes: int = 2 * 1024 * 1024) -> dict:
    try:
        safe_path = _contained_path(root, path)
    except PermissionError:
        return {}
    if not safe_path.is_file():
        return {}
    try:
        if safe_path.stat().st_size > max_bytes:
            return {}
        payload = json.loads(safe_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _workspace_summary(run_dir: Path) -> dict:
    run_dir = _contained_path(RESULTS_DIR, run_dir)
    state_path = run_dir / "workflow_state.json"
    raw = _read_small_json(state_path, run_dir)
    state_summary = {
        key: raw.get(key) for key in _STATE_SUMMARY_KEYS if key in raw
    }
    run_count = 0
    for directory in run_dir.iterdir():
        if not directory.is_dir() or not _RUN_DIR_PATTERN.match(directory.name):
            continue
        try:
            _safe_workspace_child(run_dir, directory)
        except PermissionError:
            continue
        run_count += 1
    pdfs = _safe_named_files(run_dir, run_dir, "*.pdf")
    try:
        frontend_run_dir = _safe_workspace_child(
            run_dir, "artifacts/frontend_runs"
        )
    except PermissionError:
        frontend_run_dir = None
    frontend_runs = (
        len(_safe_named_files(run_dir, frontend_run_dir, "*.json"))
        if frontend_run_dir is not None
        else 0
    )
    return {
        "workspace": str(run_dir.resolve()),
        "workspace_rel": run_dir.resolve().relative_to(RESULTS_DIR).as_posix(),
        "experiment": run_dir.parent.name,
        "run_name": run_dir.name,
        "state": state_summary,
        "state_exists": state_path.exists(),
        "run_count": run_count,
        "artifacts": {
            "pdfs": pdfs,
            "frontend_runs": frontend_runs,
        },
    }


def list_workspaces(results_root: str):
    root = Path(results_root).expanduser().resolve()
    if not root.is_dir():
        return []
    workspaces = []
    for exp_dir in sorted(root.iterdir()):
        if not exp_dir.is_dir() or exp_dir.name.startswith("."):
            continue
        try:
            _contained_path(root, exp_dir)
        except PermissionError:
            continue
        for run_dir in sorted(exp_dir.iterdir()):
            if not run_dir.is_dir() or run_dir.name.startswith("."):
                continue
            try:
                _contained_path(root, run_dir)
                workspaces.append(_workspace_summary(run_dir))
            except (OSError, PermissionError, ValueError):
                continue
    return workspaces


def _safe_workspace_path(workspace_rel: str) -> Path:
    if not isinstance(workspace_rel, str) or not workspace_rel.strip() or "\x00" in workspace_rel:
        raise PermissionError("forbidden")
    return _contained_path(RESULTS_DIR, workspace_rel.strip())


def _safe_results_file(path_suffix: str) -> Path:
    if not isinstance(path_suffix, str) or not path_suffix.strip() or "\x00" in path_suffix:
        raise PermissionError("forbidden")
    return _contained_path(RESULTS_DIR, path_suffix.strip())


def _workspace_database_status(workspace: Path) -> dict:
    empty = {
        "workflow": None,
        "approvals": [],
        "claim_coverage": {
            "passed": False,
            "claim_count": 0,
            "supported_count": 0,
            "coverage_ratio": 0.0,
            "failures": [],
        },
    }
    try:
        database = _safe_workspace_child(workspace, ".paperforge/paperforge.db")
    except PermissionError:
        return empty
    if not database.is_file():
        return empty

    try:
        connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
    except sqlite3.Error:
        return empty
    try:
        table_names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        workflow = None
        if "workflows" in table_names:
            row = connection.execute(
                """
                SELECT id, profile, status, checkpoint, updated_at, error_code, metadata_json
                FROM workflows ORDER BY updated_at DESC LIMIT 1
                """
            ).fetchone()
            if row is not None:
                workflow = dict(row)
                try:
                    workflow["metadata"] = json.loads(
                        workflow.pop("metadata_json") or "{}"
                    )
                except json.JSONDecodeError:
                    workflow["metadata"] = {}

        approvals = []
        if "approvals" in table_names:
            rows = connection.execute(
                """
                SELECT id, proposal_id, status, approved_by, approved_at, scope_json
                FROM approvals ORDER BY approved_at DESC LIMIT 100
                """
            ).fetchall()
            for row in rows:
                item = dict(row)
                try:
                    item["scope"] = json.loads(item.pop("scope_json") or "{}")
                except json.JSONDecodeError:
                    item["scope"] = {}
                approvals.append(item)

        claim_rows = []
        if {"claims", "claim_evidence"}.issubset(table_names):
            claim_rows = connection.execute(
                """
                SELECT c.id, c.status,
                       SUM(CASE WHEN ce.relation IN ('supports', 'qualifies') THEN 1 ELSE 0 END)
                           AS supporting_edges,
                       SUM(CASE WHEN ce.relation = 'contradicts' THEN 1 ELSE 0 END)
                           AS contradictions
                FROM claims c
                LEFT JOIN claim_evidence ce ON ce.claim_id = c.id
                GROUP BY c.id, c.status
                ORDER BY c.id
                """
            ).fetchall()
    except sqlite3.Error:
        return empty
    finally:
        connection.close()

    failures = []
    supported_count = 0
    for row in claim_rows:
        status = str(row["status"])
        supporting_edges = int(row["supporting_edges"] or 0)
        contradictions = int(row["contradictions"] or 0)
        reason = None
        if status not in _SUPPORTED_CLAIM_STATUSES:
            reason = f"status={status}"
        elif supporting_edges == 0:
            reason = "no supporting evidence"
        elif contradictions:
            reason = "contradicted"
        else:
            supported_count += 1
        if reason:
            failures.append({"claim_id": str(row["id"]), "reason": reason})
    claim_count = len(claim_rows)
    return {
        "workflow": workflow,
        "approvals": approvals,
        "claim_coverage": {
            "passed": claim_count > 0 and not failures,
            "claim_count": claim_count,
            "supported_count": supported_count,
            "coverage_ratio": round(supported_count / claim_count, 4)
            if claim_count
            else 0.0,
            "failures": failures,
        },
    }


def _provider_status(workspace: Path) -> dict:
    config = _read_small_json(workspace / "workspace_config.json", workspace)
    model = str(config.get("writeup_model") or "gpt-5.4-xhigh")
    stage = "writeup"
    try:
        provider_config = ProviderRegistry().resolve(model, stage=stage)
        public_config = provider_config.public_dict()
    except Exception as exc:
        return {
            "status": "CONFIG_ERROR",
            "model": model,
            "detail": redact_secrets(str(exc)).splitlines()[0][:240],
        }

    canonical_name = (
        f"PAPERFORGE_CREDENTIAL_{provider_config.credential_alias.upper().replace('-', '_')}"
    )
    legacy_names = (
        ("OPENAI_WRITEUP_API_KEY", "OPENAI_API_KEY")
        if provider_config.credential_alias == "openai_writeup"
        else ("OPENAI_API_KEY", "OPENAI_WRITEUP_API_KEY")
    )
    credential_configured = any(
        bool(os.environ.get(name)) for name in (canonical_name, *legacy_names)
    )
    result = {
        "status": "CONFIGURED" if credential_configured else "AUTH_UNCHECKED",
        "credential_configured": credential_configured,
        "credential_check_scope": "environment_only",
        "config": public_config,
    }
    preflight = _read_small_json(
        workspace / ".paperforge" / "provider_status.json", workspace
    )
    if preflight:
        result["preflight"] = {
            key: redact_secrets(str(preflight[key]))[:500]
            if key == "detail"
            else preflight[key]
            for key in ("provider", "model", "status", "detail", "response_received")
            if key in preflight
        }
    return result


def _artifact_previews(workspace: Path, workspace_rel: str) -> list[dict]:
    previews: list[dict] = []
    for current_root, directory_names, file_names in os.walk(
        workspace, followlinks=False
    ):
        directory_names[:] = sorted(
            name
            for name in directory_names
            if not name.startswith(".")
            and name not in {"__pycache__", "node_modules"}
            and not (Path(current_root) / name).is_symlink()
        )
        for filename in sorted(file_names):
            if len(previews) >= 80:
                return previews
            candidate = Path(current_root) / filename
            if candidate.suffix.lower() not in _PREVIEW_SUFFIXES:
                continue
            if any(token in filename.lower() for token in ("credential", "secret", "token")):
                continue
            try:
                safe_file = _safe_workspace_child(workspace, candidate)
            except PermissionError:
                continue
            if not safe_file.is_file():
                continue
            try:
                size = safe_file.stat().st_size
            except OSError:
                continue
            relative = safe_file.relative_to(workspace).as_posix()
            suffix = safe_file.suffix.lower()
            kind = (
                "pdf"
                if suffix == ".pdf"
                else "image"
                if suffix in {".png", ".jpg", ".jpeg", ".svg"}
                else "data"
                if suffix in {".json", ".csv"}
                else "text"
            )
            previews.append(
                {
                    "path": relative,
                    "name": safe_file.name,
                    "kind": kind,
                    "size": size,
                    "url": (
                        f"/files/results/{quote(workspace_rel, safe='/')}/"
                        f"{quote(relative, safe='/')}"
                    ),
                }
            )
    return previews


def _release_gate(
    workspace: Path, claim_coverage: dict, artifact_previews: list[dict]
) -> dict:
    database = workspace / ".paperforge" / "paperforge.db"
    publication_manifests = tuple(workspace.rglob("publication.manifest.json"))
    if database.is_file() and publication_manifests:
        try:
            from paperforge.release import ReleaseVerifier

            gate = ReleaseVerifier(workspace).verify()
            return {
                "passed": gate.passed,
                "checks": {
                    key: value
                    for key, value in gate.to_dict().items()
                    if key != "details"
                },
                "details": gate.details,
                "source": "authoritative-verifier",
            }
        except Exception:
            pass

    has_pdf = any(item["kind"] == "pdf" for item in artifact_previews)
    checks = {
        "claim_gate_passed": bool(claim_coverage.get("passed")),
        "required_artifacts_present": bool(artifact_previews),
        "latex_clean_compile": has_pdf,
        "all_pdf_pages_inspected": False,
        "protected_hashes_unchanged": False,
        "secret_scan_clean": False,
        "release_manifest_verified": False,
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "source": "frontend-derived",
        "detail": "Run `paperforge release` for authoritative release validation.",
    }


def _workspace_v3_status(workspace: Path, workspace_rel: str) -> dict:
    database_status = _workspace_database_status(workspace)
    workflow = database_status["workflow"]
    profile = str((workflow or {}).get("profile") or "full")
    artifact_previews = _artifact_previews(workspace, workspace_rel)
    claim_coverage = database_status["claim_coverage"]
    status = str((workflow or {}).get("status") or "READY")
    return {
        "profile": profile,
        "available_profiles": ["writing-only", "research", "full"],
        "provider": _provider_status(workspace),
        "workflow": workflow,
        "approvals": database_status["approvals"],
        "claim_coverage": claim_coverage,
        "resume": {
            "available": bool(workflow) and status != "COMPLETED",
            "workflow_id": (workflow or {}).get("id"),
            "proposal_id": ((workflow or {}).get("metadata") or {}).get(
                "proposal_id"
            ),
            "checkpoint": (workflow or {}).get("checkpoint"),
            "status": status,
        },
        "artifact_previews": artifact_previews,
        "release_gate": _release_gate(
            workspace, claim_coverage, artifact_previews
        ),
    }


def _validated_config_patch(workspace: Path, payload: dict) -> dict:
    patch: dict = {}
    for key, value in payload.items():
        if key not in _CONFIG_KEYS:
            continue
        if key == "writeup_model":
            normalized = str(value or "").strip()
            if not _MODEL_PATTERN.fullmatch(normalized):
                raise RequestError("invalid writeup_model")
            patch[key] = normalized
        elif key == "gateway_profile":
            if value not in {"safe", "full"}:
                raise RequestError("invalid gateway_profile")
            patch[key] = value
        elif key == "existing_draft":
            if value in {None, ""}:
                patch[key] = None
                continue
            if not isinstance(value, str) or Path(value).is_absolute():
                raise RequestError("existing_draft must be workspace-relative")
            try:
                _safe_workspace_child(workspace, value)
            except PermissionError as exc:
                raise RequestError("existing_draft escapes the workspace", 403) from exc
            patch[key] = value
        elif key == "skip_chktex_fix":
            if not isinstance(value, bool):
                raise RequestError("skip_chktex_fix must be a boolean")
            patch[key] = value
        else:
            if value is None:
                patch[key] = None
            else:
                patch[key] = _valid_identifier(value, key)
    return patch


def _workspace_history_view(workspace: Path) -> dict:
    history_file = _read_small_json(
        workspace / "workspace_history.json", workspace
    )
    result = {
        "updated_at": history_file.get("updated_at"),
        "source_drafts": [],
        "templates": [],
        "migrations": [],
        "deleted_items": [],
        "frontend_runs": [],
    }
    locations = {
        "source_drafts": "source_drafts",
        "templates": "template_library",
        "migrations": "migrations",
        "deleted_items": "recycle_bin",
    }
    for key, relative in locations.items():
        try:
            parent = _safe_workspace_child(workspace, relative)
        except PermissionError:
            continue
        if not parent.is_dir():
            continue
        items = []
        for child in sorted(parent.iterdir()):
            try:
                safe_child = _safe_workspace_child(workspace, child)
                manifest = _safe_workspace_child(
                    workspace, safe_child / "manifest.json"
                )
            except PermissionError:
                continue
            payload = _read_small_json(manifest, workspace)
            if not payload:
                continue
            payload["path"] = str(safe_child)
            items.append(payload)
        items.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
        result[key] = items
    return result


def _safe_named_files(
    workspace: Path, directory: Path, pattern: str
) -> list[str]:
    try:
        safe_directory = _safe_workspace_child(
            workspace, directory, allow_root=True
        )
    except PermissionError:
        return []
    if not safe_directory.is_dir():
        return []
    names = []
    for candidate in safe_directory.glob(pattern):
        try:
            safe_file = _safe_workspace_child(workspace, candidate)
        except PermissionError:
            continue
        if safe_file.is_file():
            names.append(candidate.name)
    return sorted(names)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        print(f"[{self.address_string()}] {format % args}")

    @property
    def _server_port(self) -> int:
        return int(self.server.server_address[1])

    def _session_token(self) -> str:
        token = getattr(self.server, "paperforge_session_token", None)
        if not isinstance(token, str) or not token:
            candidate = secrets.token_urlsafe(32)
            token = self.server.__dict__.setdefault(
                "paperforge_session_token", candidate
            )
            if not isinstance(token, str) or not token:
                token = candidate
                self.server.paperforge_session_token = token
        return token

    def _valid_local_authority(self, value: str, *, origin: bool) -> bool:
        try:
            parsed = urlparse(value if origin else f"http://{value}")
            hostname = (parsed.hostname or "").lower().rstrip(".")
            port = parsed.port
        except ValueError:
            return False
        if hostname not in _LOCAL_HOSTS or parsed.username or parsed.password:
            return False
        if origin:
            if parsed.scheme != "http" or parsed.path not in {"", "/"}:
                return False
            if parsed.query or parsed.fragment:
                return False
        elif parsed.path or parsed.query or parsed.fragment:
            return False
        if port is None:
            port = 80
        return port == self._server_port

    def _request_allowed(self, *, mutate: bool = False) -> bool:
        if len(self.headers.get_all("Host", [])) != 1:
            self._send_json({"error": "invalid host header"}, 403)
            return False
        host = self.headers.get("Host", "")
        if not self._valid_local_authority(host, origin=False):
            self._send_json({"error": "forbidden host"}, 403)
            return False
        origin = self.headers.get("Origin")
        if origin and not self._valid_local_authority(origin, origin=True):
            self._send_json({"error": "forbidden origin"}, 403)
            return False
        if self.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
            self._send_json({"error": "cross-site requests are forbidden"}, 403)
            return False
        if not mutate:
            return True

        header_token = self.headers.get(_CSRF_HEADER, "")
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            self._send_json({"error": "invalid session cookie"}, 403)
            return False
        cookie_token = (
            cookie[_SESSION_COOKIE].value if _SESSION_COOKIE in cookie else ""
        )
        expected = self._session_token()
        if not (
            secrets.compare_digest(header_token, expected)
            and secrets.compare_digest(cookie_token, expected)
        ):
            self._send_json({"error": "invalid CSRF session token"}, 403)
            return False
        return True

    def _cors_origin(self) -> str:
        origin = self.headers.get("Origin", "")
        if origin and self._valid_local_authority(origin, origin=True):
            return origin
        return f"http://127.0.0.1:{self._server_port}"

    def _security_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", self._cors_origin())
        self.send_header("Access-Control-Allow-Credentials", "true")
        self.send_header("Vary", "Origin")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; "
            "script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "object-src 'self'; frame-ancestors 'none'; base-uri 'none'",
        )

    def _send_json(
        self,
        data: object,
        status: int = 200,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, *, trusted_frontend: bool = False) -> None:
        suffix = path.suffix.lower()
        mime = {
            ".html": (
                "text/html; charset=utf-8"
                if trusted_frontend
                else "application/octet-stream"
            ),
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".log": "text/plain; charset=utf-8",
            ".txt": "text/plain; charset=utf-8",
            ".tex": "text/plain; charset=utf-8",
            ".pdf": "application/pdf",
        }.get(suffix, "application/octet-stream")
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        if suffix == ".html" and not trusted_frontend:
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{path.name.replace(chr(34), "")}"',
            )
            self.send_header("Content-Security-Policy", "sandbox; default-src 'none'")
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_empty(self, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self._security_headers()
        self.end_headers()

    def _read_body(self, max_bytes: int) -> bytes:
        if self.headers.get("Transfer-Encoding"):
            raise RequestError("transfer encoding is not supported", 411)
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length or "0")
        except ValueError as exc:
            raise RequestError("invalid Content-Length") from exc
        if length < 0:
            raise RequestError("invalid Content-Length")
        if length > max_bytes:
            raise RequestError("request body too large", 413)
        return self.rfile.read(length) if length else b""

    def _read_json_body(self) -> dict:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type not in {"", "application/json"}:
            raise RequestError("Content-Type must be application/json", 415)
        raw = self._read_body(_MAX_JSON_BODY)
        if not raw:
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RequestError("invalid JSON body") from exc
        if not isinstance(payload, dict):
            raise RequestError("JSON body must be an object")
        return payload

    def _read_multipart_files(self) -> list[dict]:
        content_type = self.headers.get("Content-Type", "")
        if "\r" in content_type or "\n" in content_type:
            raise RequestError("invalid multipart Content-Type")
        if not content_type.lower().startswith("multipart/form-data;"):
            raise RequestError("Content-Type must be multipart/form-data", 415)
        raw = self._read_body(_MAX_UPLOAD_BODY)
        if not raw:
            raise RequestError("multipart body is empty")
        envelope = (
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode()
            + raw
        )
        try:
            message = BytesParser(policy=email_policy.default).parsebytes(envelope)
        except Exception as exc:
            raise RequestError("invalid multipart body") from exc
        if message.defects or not message.is_multipart():
            raise RequestError("invalid multipart body")

        uploaded: list[dict] = []
        for item in message.iter_parts():
            if item.get_content_disposition() != "form-data":
                continue
            filename = item.get_filename()
            if not filename:
                continue
            if len(uploaded) >= _MAX_UPLOAD_FILES:
                raise RequestError("too many uploaded files", 413)
            if "\x00" in filename:
                raise RequestError("invalid upload filename")
            content = item.get_payload(decode=True)
            if content is None:
                content = b""
            uploaded.append(
                {
                    "field": str(
                        item.get_param(
                            "name", header="content-disposition", failobj=""
                        )
                    ),
                    "filename": str(filename),
                    "content": content,
                }
            )
        return uploaded

    def do_GET(self) -> None:
        if not self._request_allowed():
            return
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/api/session":
            token = self._session_token()
            self._send_json(
                {
                    "csrf_token": token,
                    "csrf_header": _CSRF_HEADER,
                    "session_cookie": _SESSION_COOKIE,
                },
                extra_headers={
                    "Cache-Control": "no-store",
                    "Set-Cookie": (
                        f"{_SESSION_COOKIE}={token}; Path=/; "
                        "HttpOnly; SameSite=Strict"
                    ),
                },
            )
            return

        if path == "/api/system/status":
            self._send_json(
                {
                    "service": "PaperForge local UI",
                    "python": {
                        "version": ".".join(
                            str(part) for part in sys.version_info[:3]
                        ),
                        "supported_versions": ["3.10", "3.11", "3.12"],
                    },
                    "cli": {
                        "executable": [sys.executable, "-m", "paperforge"],
                        "actions": [
                            "preflight",
                            "run",
                            "approve",
                            "resume",
                            "publish",
                            "release",
                            "status",
                        ],
                    },
                    "security": {
                        "loopback_only": True,
                        "csrf_required": True,
                        "workspace_root": str(RESULTS_DIR),
                    },
                }
            )
            return

        if path == "/api/workspaces":
            self._send_json(
                {
                    "workspaces": list_workspaces(str(RESULTS_DIR)),
                    "results_root": str(RESULTS_DIR),
                }
            )
            return

        if path.startswith("/api/workspace/") and path.endswith("/log"):
            workspace_rel = unquote(path[len("/api/workspace/") : -len("/log")].strip("/"))
            try:
                workspace = _safe_workspace_path(workspace_rel)
            except PermissionError:
                self._send_json({"error": "forbidden"}, 403)
                return
            workspace_rel = workspace.relative_to(RESULTS_DIR).as_posix()
            query = parse_qs(parsed.query)
            run_id = (query.get("run_id") or [""])[0]
            try:
                offset = int((query.get("offset") or ["0"])[0] or 0)
                if offset < 0:
                    raise ValueError
                payload = PROCESS_MANAGER.read_log(workspace_rel, run_id, offset=offset)
            except FileNotFoundError:
                self._send_json({"error": "run not found"}, 404)
                return
            except ValueError:
                self._send_json({"error": "invalid log request"}, 400)
                return
            self._send_json(payload)
            return

        if path.startswith("/api/workspace/") and path.endswith("/status"):
            workspace_rel = unquote(
                path[len("/api/workspace/") : -len("/status")].strip("/")
            )
            try:
                ws = _safe_workspace_path(workspace_rel)
            except PermissionError:
                self._send_json({"error": "forbidden"}, 403)
                return
            if not ws.is_dir():
                self._send_json(
                    {"error": f"workspace not found: {workspace_rel}"}, 404
                )
                return
            workspace_rel = ws.relative_to(RESULTS_DIR).as_posix()
            self._send_json(
                {
                    "workspace_rel": workspace_rel,
                    **_workspace_v3_status(ws, workspace_rel),
                }
            )
            return

        if path.startswith("/api/workspace/") and path.endswith("/history"):
            workspace_rel = unquote(path[len("/api/workspace/") : -len("/history")].strip("/"))
            try:
                ws = _safe_workspace_path(workspace_rel)
            except PermissionError:
                self._send_json({"error": "forbidden"}, 403)
                return
            if not ws.is_dir():
                self._send_json({"error": f"workspace not found: {workspace_rel}"}, 404)
                return
            workspace_rel = ws.relative_to(RESULTS_DIR).as_posix()
            history = _workspace_history_view(ws)
            self._send_json({"workspace_rel": workspace_rel, "history": history})
            return

        if path.startswith("/api/workspace/"):
            workspace_rel = unquote(path[len("/api/workspace/") :].strip("/"))
            try:
                ws = _safe_workspace_path(workspace_rel)
            except PermissionError:
                self._send_json({"error": "forbidden"}, 403)
                return
            if not ws.is_dir():
                self._send_json({"error": f"workspace not found: {workspace_rel}"}, 404)
                return
            workspace_rel = ws.relative_to(RESULTS_DIR).as_posix()

            state = _read_small_json(ws / "workflow_state.json", ws)
            notes = ""
            try:
                notes_file = _safe_workspace_child(ws, "notes.txt")
                if notes_file.is_file() and notes_file.stat().st_size <= 1024 * 1024:
                    notes = notes_file.read_text(
                        encoding="utf-8", errors="replace"
                    )[:4000]
            except (OSError, PermissionError):
                pass
            history = _workspace_history_view(ws)
            run_dirs = []
            for candidate in ws.iterdir():
                if not candidate.is_dir() or not _RUN_DIR_PATTERN.match(candidate.name):
                    continue
                try:
                    run_dirs.append(_safe_workspace_child(ws, candidate))
                except PermissionError:
                    continue
            run_dirs.sort(
                key=lambda directory: int(
                    _RUN_DIR_PATTERN.match(directory.name).group(1)
                )
            )
            runs = []
            for rd in run_dirs:
                try:
                    fi = _safe_workspace_child(ws, rd / "final_info.json")
                except PermissionError:
                    continue
                run_data = {"name": rd.name, "has_result": fi.exists()}
                if fi.exists():
                    final_info = _read_small_json(fi, ws)
                    if final_info:
                        run_data["keys"] = list(final_info.keys())
                runs.append(run_data)
            latex_dir = ws / "latex"
            latex = {}
            try:
                latex_dir = _safe_workspace_child(ws, latex_dir)
            except PermissionError:
                latex_dir = None
            if latex_dir is not None and latex_dir.is_dir():
                latex = {
                    "tex_files": _safe_named_files(ws, latex_dir, "*.tex"),
                    "pdf_files": _safe_named_files(ws, latex_dir, "*.pdf"),
                    "log_files": _safe_named_files(ws, latex_dir, "*.log"),
                }
            config = _read_small_json(ws / "workspace_config.json", ws)
            v3_status = _workspace_v3_status(ws, workspace_rel)
            self._send_json(
                {
                    "workspace": str(ws),
                    "workspace_rel": workspace_rel,
                    "state": state,
                    "config": config,
                    "notes_preview": notes,
                    "history": history,
                    "source_drafts": history.get("source_drafts", []),
                    "templates": history.get("templates", []),
                    "migrations": history.get("migrations", []),
                    "deleted_items": history.get("deleted_items", []),
                    "runs": runs,
                    "latex": latex,
                    "root_pdf_files": _safe_named_files(ws, ws, "*.pdf"),
                    "frontend_runs": PROCESS_MANAGER.list_run_records(workspace_rel),
                    "v3": v3_status,
                    "profile": v3_status["profile"],
                    "provider_status": v3_status["provider"],
                    "approvals": v3_status["approvals"],
                    "claim_coverage": v3_status["claim_coverage"],
                    "resume": v3_status["resume"],
                    "artifact_previews": v3_status["artifact_previews"],
                    "release_gate": v3_status["release_gate"],
                }
            )
            return

        if path.startswith("/files/results/"):
            rel = unquote(path[len("/files/results/") :].lstrip("/"))
            try:
                file_path = _safe_results_file(rel)
            except PermissionError:
                self._send_empty(403)
                return
            if not file_path.is_file():
                self._send_empty(404)
                return
            self._send_file(file_path)
            return

        if path == "/":
            try:
                index = _contained_path(FRONTEND_DIR, "index.html")
            except PermissionError:
                self._send_empty(404)
                return
            self._send_file(index, trusted_frontend=True)
            return

        try:
            file_path = _contained_path(FRONTEND_DIR, path.lstrip("/"))
        except PermissionError:
            self._send_empty(404)
            return
        if file_path.is_file():
            self._send_file(file_path, trusted_frontend=True)
            return

        self._send_empty(404)

    def do_PUT(self) -> None:
        if not self._request_allowed(mutate=True):
            return
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if not path.startswith("/api/workspace/") or not path.endswith("/config"):
            self._send_empty(404)
            return
        workspace_rel = unquote(path[len("/api/workspace/") : -len("/config")].strip("/"))
        try:
            ws = _safe_workspace_path(workspace_rel)
        except PermissionError:
            self._send_json({"error": "forbidden"}, 403)
            return
        if not ws.is_dir():
            self._send_json({"error": "workspace not found"}, 404)
            return
        workspace_rel = ws.relative_to(RESULTS_DIR).as_posix()
        try:
            payload = self._read_json_body()
            _ensure_workspace_targets(ws, ["workspace_config.json"])
            merged = _read_small_json(ws / "workspace_config.json", ws)
            merged.update(_validated_config_patch(ws, payload))
            saved = save_workspace_config(ws, merged)
        except RequestError as exc:
            self._send_json({"error": str(exc)}, exc.status)
            return
        except PermissionError:
            self._send_json({"error": "forbidden"}, 403)
            return
        except Exception as exc:
            self._send_json({"error": redact_secrets(str(exc))}, 400)
            return
        self._send_json({"workspace_rel": workspace_rel, "config": saved})

    def do_POST(self) -> None:
        if not self._request_allowed(mutate=True):
            return
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path.startswith("/api/workspace/") and path.endswith("/drafts/import"):
            workspace_rel = unquote(path[len("/api/workspace/") : -len("/drafts/import")].strip("/"))
            try:
                ws = _safe_workspace_path(workspace_rel)
            except PermissionError:
                self._send_json({"error": "forbidden"}, 403)
                return
            if not ws.is_dir():
                self._send_json({"error": "workspace not found"}, 404)
                return
            workspace_rel = ws.relative_to(RESULTS_DIR).as_posix()
            try:
                _ensure_workspace_targets(
                    ws,
                    [
                        "source_drafts",
                        "workspace_config.json",
                        "workspace_history.json",
                    ],
                )
                uploaded = self._read_multipart_files()
                manifest = import_source_draft(ws, uploaded)
            except RequestError as exc:
                self._send_json({"error": str(exc)}, exc.status)
                return
            except PermissionError:
                self._send_json({"error": "forbidden"}, 403)
                return
            except Exception as exc:
                self._send_json({"error": redact_secrets(str(exc))}, 400)
                return
            self._send_json(
                {
                    "workspace_rel": workspace_rel,
                    "draft": manifest,
                    "history": _workspace_history_view(ws),
                },
                201,
            )
            return

        if path.startswith("/api/workspace/") and path.endswith("/templates/import"):
            workspace_rel = unquote(path[len("/api/workspace/") : -len("/templates/import")].strip("/"))
            try:
                ws = _safe_workspace_path(workspace_rel)
            except PermissionError:
                self._send_json({"error": "forbidden"}, 403)
                return
            if not ws.is_dir():
                self._send_json({"error": "workspace not found"}, 404)
                return
            workspace_rel = ws.relative_to(RESULTS_DIR).as_posix()
            try:
                _ensure_workspace_targets(
                    ws,
                    [
                        "template_library",
                        "workspace_config.json",
                        "workspace_history.json",
                    ],
                )
                uploaded = self._read_multipart_files()
                manifest = import_template_directory(ws, uploaded)
            except RequestError as exc:
                self._send_json({"error": str(exc)}, exc.status)
                return
            except PermissionError:
                self._send_json({"error": "forbidden"}, 403)
                return
            except Exception as exc:
                self._send_json({"error": redact_secrets(str(exc))}, 400)
                return
            self._send_json(
                {
                    "workspace_rel": workspace_rel,
                    "template": manifest,
                    "history": _workspace_history_view(ws),
                },
                201,
            )
            return

        if path.startswith("/api/workspace/") and path.endswith("/migrations"):
            workspace_rel = unquote(path[len("/api/workspace/") : -len("/migrations")].strip("/"))
            try:
                ws = _safe_workspace_path(workspace_rel)
            except PermissionError:
                self._send_json({"error": "forbidden"}, 403)
                return
            if not ws.is_dir():
                self._send_json({"error": "workspace not found"}, 404)
                return
            workspace_rel = ws.relative_to(RESULTS_DIR).as_posix()
            try:
                payload = self._read_json_body()
                source_draft_id = _valid_identifier(
                    payload.get("source_draft_id"), "source_draft_id"
                )
                template_id = _valid_identifier(
                    payload.get("template_id"), "template_id"
                )
                output_name = _valid_identifier(
                    payload.get("output_name"), "output_name"
                )
                result = PROCESS_MANAGER.start_run(
                    {
                        "entry": "migration",
                        "workspace_rel": workspace_rel,
                        "source_draft_id": source_draft_id,
                        "template_id": template_id,
                        "output_name": output_name,
                        "template": payload.get("publish_template") or "generic",
                    }
                )
            except RequestError as exc:
                self._send_json({"error": str(exc)}, exc.status)
                return
            except Exception as exc:
                self._send_json({"error": redact_secrets(str(exc))}, 400)
                return
            self._send_json(result, 201)
            return

        if path.startswith("/api/workspace/") and "/artifacts/" in path and path.endswith("/recycle"):
            prefix = "/api/workspace/"
            workspace_and_rest = unquote(path[len(prefix) :].strip("/"))
            workspace_rel, _, rest = workspace_and_rest.partition("/artifacts/")
            kind, _, tail = rest.partition("/")
            item_id = tail[: -len("/recycle")] if tail.endswith("/recycle") else ""
            try:
                ws = _safe_workspace_path(workspace_rel)
            except PermissionError:
                self._send_json({"error": "forbidden"}, 403)
                return
            if not ws.is_dir():
                self._send_json({"error": "workspace not found"}, 404)
                return
            workspace_rel = ws.relative_to(RESULTS_DIR).as_posix()
            try:
                if kind not in {"source_drafts", "templates", "migrations"}:
                    raise RequestError("unsupported artifact kind")
                item_id = _valid_identifier(item_id, "item_id")
                directory = {
                    "source_drafts": "source_drafts",
                    "templates": "template_library",
                    "migrations": "migrations",
                }[kind]
                _ensure_workspace_targets(
                    ws,
                    [
                        directory,
                        f"{directory}/{item_id}",
                        "recycle_bin",
                        "workspace_history.json",
                    ],
                )
                result = recycle_workspace_artifact(ws, kind, item_id)
            except RequestError as exc:
                self._send_json({"error": str(exc)}, exc.status)
                return
            except PermissionError:
                self._send_json({"error": "forbidden"}, 403)
                return
            except FileNotFoundError:
                self._send_json({"error": "artifact not found"}, 404)
                return
            except Exception as exc:
                self._send_json({"error": redact_secrets(str(exc))}, 400)
                return
            self._send_json(
                {
                    "workspace_rel": workspace_rel,
                    "deleted": result,
                    "history": _workspace_history_view(ws),
                }
            )
            return

        if path.startswith("/api/workspace/") and "/recycle/" in path and path.endswith("/restore"):
            prefix = "/api/workspace/"
            workspace_and_rest = unquote(path[len(prefix) :].strip("/"))
            workspace_rel, _, rest = workspace_and_rest.partition("/recycle/")
            recycle_id = rest[: -len("/restore")] if rest.endswith("/restore") else ""
            try:
                ws = _safe_workspace_path(workspace_rel)
            except PermissionError:
                self._send_json({"error": "forbidden"}, 403)
                return
            if not ws.is_dir():
                self._send_json({"error": "workspace not found"}, 404)
                return
            workspace_rel = ws.relative_to(RESULTS_DIR).as_posix()
            try:
                recycle_id = _valid_identifier(recycle_id, "recycle_id")
                _ensure_workspace_targets(
                    ws,
                    [
                        "recycle_bin",
                        f"recycle_bin/{recycle_id}",
                        f"recycle_bin/{recycle_id}/manifest.json",
                        "workspace_history.json",
                    ],
                )
                manifest = _read_small_json(
                    ws / "recycle_bin" / recycle_id / "manifest.json", ws
                )
                if not manifest:
                    raise FileNotFoundError(recycle_id)
                original_relpath = str(manifest.get("original_relpath") or "")
                payload_name = str(manifest.get("payload_name") or "")
                if Path(original_relpath).is_absolute() or Path(payload_name).name != payload_name:
                    raise RequestError("invalid recycle manifest")
                _safe_workspace_child(ws, original_relpath)
                _safe_workspace_child(
                    ws, Path("recycle_bin") / recycle_id / payload_name
                )
                result = restore_recycled_artifact(ws, recycle_id)
            except RequestError as exc:
                self._send_json({"error": str(exc)}, exc.status)
                return
            except PermissionError:
                self._send_json({"error": "forbidden"}, 403)
                return
            except FileNotFoundError:
                self._send_json({"error": "recycle item not found"}, 404)
                return
            except Exception as exc:
                self._send_json({"error": redact_secrets(str(exc))}, 400)
                return
            self._send_json(
                {
                    "workspace_rel": workspace_rel,
                    "restored": result,
                    "history": _workspace_history_view(ws),
                }
            )
            return

        try:
            payload = self._read_json_body()
        except RequestError as exc:
            self._send_json({"error": str(exc)}, exc.status)
            return

        if path == "/api/runs":
            try:
                result = PROCESS_MANAGER.start_run(payload)
            except ValueError as exc:
                self._send_json({"error": redact_secrets(str(exc))}, 400)
                return
            self._send_json(result, 201)
            return

        if path.startswith("/api/runs/") and path.endswith("/action"):
            raw_run_id = unquote(path[len("/api/runs/") : -len("/action")].strip("/"))
            action = str(payload.get("action") or "").strip().lower()
            try:
                run_id = _valid_identifier(raw_run_id, "run_id")
                if action == "pause":
                    result = PROCESS_MANAGER.pause_run(run_id)
                elif action == "resume":
                    result = PROCESS_MANAGER.resume_run(run_id)
                elif action == "stop":
                    result = PROCESS_MANAGER.stop_run(run_id)
                else:
                    self._send_json({"error": "unsupported action"}, 400)
                    return
            except RequestError as exc:
                self._send_json({"error": str(exc)}, exc.status)
                return
            except KeyError:
                self._send_json({"error": "run not found"}, 404)
                return
            except RuntimeError as exc:
                self._send_json({"error": str(exc)}, 400)
                return
            self._send_json(result)
            return

        self._send_empty(404)

    def do_OPTIONS(self) -> None:
        if not self._request_allowed():
            return
        self._send_empty(405)

    def do_DELETE(self) -> None:
        if not self._request_allowed(mutate=True):
            return
        self._send_empty(405)

    def do_PATCH(self) -> None:
        if not self._request_allowed(mutate=True):
            return
        self._send_empty(405)


def main() -> None:
    parser = argparse.ArgumentParser(description="PaperForge 前端服务器")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    if args.host.lower().rstrip(".") not in {"127.0.0.1", "localhost"}:
        parser.error("--host must be a loopback address (127.0.0.1 or localhost)")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.paperforge_session_token = secrets.token_urlsafe(32)
    print(f"PaperForge 工作台已启动: http://{args.host}:{args.port}")
    print("Ctrl+C 退出")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")


if __name__ == "__main__":
    main()
