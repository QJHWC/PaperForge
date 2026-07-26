from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any

from engine.secret_redaction import contains_secret
from paperforge.path_safety import safe_mkdir

from .contracts import JobSpec

COMPUTE_BINDING_SCHEMA = "paperforge.compute-binding/v1"
COMPUTE_JOB_MANIFEST_SCHEMA = "paperforge.compute-job/v1"
SUPPORTED_COMPUTE_BACKENDS = frozenset(
    {"local", "docker", "ssh", "slurm", "kubernetes", "cloud-ssh"}
)
_SKIPPED_INPUT_PARTS = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".paperforge",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "artifacts",
        "checkpoints",
        "logs",
        "output",
        "outputs",
        "results",
        "runs",
    }
)
_REMOTE_EXECUTION_BACKENDS = frozenset(
    {"docker", "kubernetes", "ssh", "cloud-ssh", "slurm"}
)
_IMAGE_DIGEST = re.compile(r"^.+@sha256:[0-9a-fA-F]{64}$")


class ComputeBindingError(ValueError):
    """Raised when an executable compute request cannot be bound immutably."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json_sha256(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _reject_symlink_components(path: Path, root: Path) -> None:
    if not _is_within(path, root):
        raise ComputeBindingError("compute path leaves the controlled workspace")
    relative = path.relative_to(root)
    current = root
    if root.is_symlink():
        raise ComputeBindingError("compute workspace must not be a symbolic link")
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ComputeBindingError(
                f"compute path contains a symbolic link: {relative.as_posix()}"
            )


def _resolve_controlled_path(
    raw: str | Path,
    *,
    workspace: Path,
    base: Path,
    require_file: bool = False,
    require_directory: bool = False,
) -> Path:
    candidate = Path(raw).expanduser()
    lexical = candidate if candidate.is_absolute() else base / candidate
    _reject_symlink_components(lexical.absolute(), workspace)
    resolved = lexical.resolve(strict=True)
    if not _is_within(resolved, workspace):
        raise ComputeBindingError("compute path leaves the controlled workspace")
    if require_file and not resolved.is_file():
        raise ComputeBindingError(f"compute input is not a file: {raw}")
    if require_directory and not resolved.is_dir():
        raise ComputeBindingError(f"compute worktree is not a directory: {raw}")
    return resolved


def _resolve_executable(command: tuple[str, ...], worktree: Path) -> Path:
    raw = command[0]
    candidate = Path(raw).expanduser()
    if candidate.is_absolute() or len(candidate.parts) > 1:
        executable = candidate if candidate.is_absolute() else worktree / candidate
        resolved = executable.resolve(strict=True)
    else:
        located = shutil.which(raw)
        if located is None:
            raise ComputeBindingError(f"compute executable was not found: {raw}")
        executable = Path(located)
        if executable.is_symlink():
            executable = executable.resolve(strict=True)
        resolved = executable.resolve(strict=True)
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise ComputeBindingError(f"compute executable is not executable: {resolved}")
    return resolved


def _iter_input_files(
    path: Path,
    *,
    excluded_paths: set[Path] | None = None,
) -> list[Path]:
    if path.is_file():
        return [path]
    excluded = excluded_paths or set()
    files = []
    for candidate in path.rglob("*"):
        relative = candidate.relative_to(path)
        if any(part in _SKIPPED_INPUT_PARTS for part in relative.parts):
            continue
        absolute = candidate.absolute()
        if any(
            absolute == excluded_path or excluded_path in absolute.parents
            for excluded_path in excluded
        ):
            continue
        if candidate.is_symlink():
            raise ComputeBindingError(
                f"declared compute input contains a symbolic link: {candidate}"
            )
        if candidate.is_file():
            files.append(candidate)
    return sorted(files)


def _file_records(
    paths: set[Path],
    *,
    workspace: Path,
    excluded_paths: set[Path] | None = None,
) -> list[dict[str, Any]]:
    records = []
    for path in sorted(paths):
        _reject_symlink_components(path, workspace)
        for file_path in _iter_input_files(
            path,
            excluded_paths=excluded_paths,
        ):
            records.append(
                {
                    "path": str(file_path),
                    "sha256": _sha256_file(file_path),
                    "size_bytes": file_path.stat().st_size,
                }
            )
    return records


def _git_identity(worktree: Path, workspace: Path) -> dict[str, Any] | None:
    def run(*arguments: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(worktree), *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return completed.stdout.strip()

    try:
        root = Path(run("rev-parse", "--show-toplevel")).resolve(strict=True)
        if not _is_within(root, workspace):
            return None
        return {
            "root": str(root),
            "commit": run("rev-parse", "HEAD"),
            "tree": run("rev-parse", "HEAD^{tree}"),
        }
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return None


def _resolve_tool(raw: str) -> Path:
    candidate = Path(raw).expanduser()
    located = (
        str(candidate)
        if candidate.is_absolute() or len(candidate.parts) > 1
        else shutil.which(raw)
    )
    if not located:
        raise ComputeBindingError(f"compute backend executable was not found: {raw}")
    resolved = Path(located).resolve(strict=True)
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise ComputeBindingError(
            f"compute backend executable is not executable: {resolved}"
        )
    return resolved


def _canonical_compute_config(
    backend: str,
    config: Mapping[str, Any],
    *,
    worktree: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    normalized = json.loads(json.dumps(dict(config)))
    dependencies: list[dict[str, Any]] = []
    if backend == "local":
        for field, module_path in (
            ("local_backend", Path(__file__).with_name("local.py")),
            (
                "local_supervisor",
                Path(__file__).with_name("_local_supervisor.py"),
            ),
            (
                "windows_appcontainer",
                Path(__file__).with_name("_windows_appcontainer.py"),
            ),
        ):
            dependencies.append(
                {
                    "kind": "backend_module",
                    "field": field,
                    "path": str(module_path),
                    "sha256": _sha256_file(module_path),
                }
            )
        if platform.system() == "Darwin":
            sandbox = _resolve_tool("sandbox-exec")
            dependencies.append(
                {
                    "kind": "backend_executable",
                    "field": "sandbox",
                    "path": str(sandbox),
                    "sha256": _sha256_file(sandbox),
                }
            )
        elif platform.system() == "Linux":
            sandbox = _resolve_tool("bwrap")
            dependencies.append(
                {
                    "kind": "backend_executable",
                    "field": "sandbox",
                    "path": str(sandbox),
                    "sha256": _sha256_file(sandbox),
                }
            )
        elif platform.system() == "Windows":
            for field, executable in (
                ("powershell", "powershell.exe"),
                ("icacls", "icacls.exe"),
            ):
                tool = _resolve_tool(executable)
                dependencies.append(
                    {
                        "kind": "backend_executable",
                        "field": field,
                        "path": str(tool),
                        "sha256": _sha256_file(tool),
                    }
                )
    executable_defaults = {
        "docker": {"runtime": "docker"},
        "kubernetes": {"kubectl_executable": "kubectl"},
        "slurm": {
            "sbatch_executable": "sbatch",
            "squeue_executable": "squeue",
            "sacct_executable": "sacct",
            "scancel_executable": "scancel",
            "scontrol_executable": "scontrol",
        },
        "ssh": {
            "ssh_executable": "ssh",
            "scp_executable": "scp",
        },
    }
    target = normalized
    defaults = executable_defaults.get(backend, {})
    if backend == "cloud-ssh":
        raw_ssh = normalized.get("ssh")
        if not isinstance(raw_ssh, dict):
            raise ComputeBindingError("cloud-ssh compute config requires ssh mapping")
        target = raw_ssh
        defaults = executable_defaults["ssh"]
    for key, default in defaults.items():
        tool = _resolve_tool(str(target.get(key) or default))
        target[key] = str(tool)
        dependencies.append(
            {
                "kind": "backend_executable",
                "field": key,
                "path": str(tool),
                "sha256": _sha256_file(tool),
            }
        )

    if backend == "slurm":
        runtime_value = normalized.get("container_runtime")
        image_value = normalized.get("container_image")
        if runtime_value is not None:
            runtime = _resolve_tool(str(runtime_value))
            normalized["container_runtime"] = str(runtime)
            dependencies.append(
                {
                    "kind": "backend_executable",
                    "field": "container_runtime",
                    "path": str(runtime),
                    "sha256": _sha256_file(runtime),
                }
            )
        if image_value is not None:
            lexical_image = Path(str(image_value)).expanduser()
            if lexical_image.is_symlink():
                raise ComputeBindingError(
                    "Slurm container image must not be a symbolic link"
                )
            image = lexical_image.resolve(strict=True)
            if not image.is_file():
                raise ComputeBindingError(
                    "Slurm container image must be a regular file"
                )
            normalized["container_image"] = str(image)
            dependencies.append(
                {
                    "kind": "container_image",
                    "field": "container_image",
                    "path": str(image),
                    "sha256": _sha256_file(image),
                }
            )

    ssh_config = target if backend in {"ssh", "cloud-ssh"} else None
    if ssh_config is not None:
        for field in ("known_hosts_file", "identity_file"):
            raw_path = ssh_config.get(field)
            if raw_path in (None, ""):
                if field == "known_hosts_file":
                    raise ComputeBindingError(
                        "SSH compute config requires known_hosts_file"
                    )
                continue
            lexical = Path(str(raw_path)).expanduser()
            if lexical.is_symlink():
                raise ComputeBindingError(
                    f"SSH {field} must not be a symbolic link"
                )
            resolved = lexical.resolve(strict=True)
            if not resolved.is_file():
                raise ComputeBindingError(f"SSH {field} is not a regular file")
            ssh_config[field] = str(resolved)
            dependencies.append(
                {
                    "kind": "backend_config",
                    "field": field,
                    "path": str(resolved),
                    "sha256": _sha256_file(resolved),
                }
            )

    if backend == "docker" and normalized.get("workspace_mount") is not None:
        raw_mount = Path(str(normalized["workspace_mount"])).expanduser()
        mount = (
            raw_mount if raw_mount.is_absolute() else worktree / raw_mount
        ).resolve(strict=True)
        if mount != worktree:
            raise ComputeBindingError(
                "Docker workspace_mount must equal the canonical compute worktree"
            )
        normalized["workspace_mount"] = str(mount)
    return normalized, sorted(
        dependencies,
        key=lambda record: (str(record["kind"]), str(record["field"])),
    )


def _materialize_local_snapshot(
    workspace: Path,
    worktree: Path,
    *,
    records: list[dict[str, Any]],
    snapshot_key: str,
    excluded_paths: set[Path],
) -> tuple[Path, str]:
    snapshot_parent = workspace / ".paperforge" / "compute-snapshots"
    snapshot_parent = safe_mkdir(snapshot_parent, anchor=workspace)
    snapshot = snapshot_parent / snapshot_key
    if not snapshot.exists():
        temporary = Path(
            tempfile.mkdtemp(
                prefix=f".{snapshot_key}.",
                dir=snapshot_parent,
            )
        )
        try:
            for record in records:
                source = Path(str(record["path"]))
                relative = source.relative_to(worktree)
                destination = temporary / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
            with suppress(FileExistsError):
                temporary.replace(snapshot)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    if snapshot.is_symlink() or not snapshot.is_dir():
        raise ComputeBindingError("local execution snapshot is not a safe directory")
    snapshot_records = _file_records(
        {snapshot},
        workspace=workspace,
        excluded_paths={
            snapshot / path.relative_to(worktree)
            for path in excluded_paths
        },
    )
    normalized_records = [
        {
            **record,
            "path": str(
                worktree / Path(str(record["path"])).relative_to(snapshot)
            ),
        }
        for record in snapshot_records
    ]
    snapshot_sha256 = _canonical_json_sha256({"files": normalized_records})
    expected_sha256 = _canonical_json_sha256({"files": records})
    if snapshot_sha256 != expected_sha256:
        raise ComputeBindingError(
            "local execution snapshot no longer matches approved source"
        )
    return snapshot, snapshot_sha256


def build_compute_binding(
    workspace: str | Path,
    *,
    job_spec: Mapping[str, Any],
    compute_backend: str = "local",
    compute_config: Mapping[str, Any] | None = None,
) -> tuple[JobSpec, dict[str, Any]]:
    """Canonicalize and hash everything that can change compute execution."""

    root = Path(workspace).expanduser().resolve(strict=True)
    if contains_secret(
        {
            "job_spec": dict(job_spec),
            "compute_config": dict(compute_config or {}),
        }
    ):
        raise ComputeBindingError("compute job manifest must not contain credentials")
    backend = str(compute_backend).strip().lower().replace("_", "-")
    if backend not in SUPPORTED_COMPUTE_BACKENDS:
        raise ComputeBindingError(f"unsupported compute backend: {compute_backend}")
    raw_config = dict(compute_config or {})
    spec = JobSpec.from_dict(job_spec)
    worktree = _resolve_controlled_path(
        spec.workdir,
        workspace=root,
        base=root,
        require_directory=True,
    )
    if any(part in {".git", ".paperforge"} for part in worktree.relative_to(root).parts):
        raise ComputeBindingError("compute worktree cannot be an internal state directory")
    config, config_dependencies = _canonical_compute_config(
        backend,
        raw_config,
        worktree=worktree,
    )

    if backend in _REMOTE_EXECUTION_BACKENDS:
        executable_record = {
            "scope": "remote",
            "command": str(tuple(spec.command)[0]),
        }
        canonical_command = tuple(spec.command)
    else:
        executable = _resolve_executable(tuple(spec.command), worktree)
        executable_record = {
            "scope": "local",
            "path": str(executable),
            "sha256": _sha256_file(executable),
        }
        canonical_command = (str(executable), *tuple(spec.command)[1:])
    bound_inputs: set[Path] = set()
    canonical_inputs: list[str] = []
    for raw_input in spec.inputs:
        resolved = _resolve_controlled_path(
            raw_input,
            workspace=root,
            base=worktree,
        )
        bound_inputs.add(resolved)
        canonical_inputs.append(str(resolved))

    for argument in canonical_command[1:]:
        if not argument or argument.startswith("-"):
            continue
        candidate = Path(argument)
        lexical = candidate if candidate.is_absolute() else worktree / candidate
        if lexical.exists():
            resolved = _resolve_controlled_path(
                candidate,
                workspace=root,
                base=worktree,
            )
            bound_inputs.add(resolved)

    canonical_payload = spec.to_dict()
    canonical_workdir = str(worktree)
    canonical_payload.update(
        {
            "command": list(canonical_command),
            "workdir": canonical_workdir,
            "inputs": canonical_inputs,
        }
    )
    canonical_spec = JobSpec.from_dict(canonical_payload)
    files = _file_records(bound_inputs, workspace=root)
    mutable_outputs: set[Path] = set()
    for raw_output in canonical_spec.outputs:
        output = (worktree / raw_output).absolute()
        _reject_symlink_components(output, root)
        if output.is_symlink():
            raise ComputeBindingError("compute output cannot be a symbolic link")
        mutable_outputs.add(output)
    worktree_files = _file_records(
        {worktree},
        workspace=root,
        excluded_paths=mutable_outputs,
    )
    worktree_sha256 = _canonical_json_sha256({"files": worktree_files})
    if backend in {"docker", "kubernetes"} and canonical_spec.execute:
        image = str(
            canonical_spec.backend_options.get("image")
            or config.get("image")
            or ""
        )
        if not _IMAGE_DIGEST.fullmatch(image):
            raise ComputeBindingError(
                "executable container jobs require an image pinned by sha256 digest"
            )
    if backend in {"ssh", "cloud-ssh"} and canonical_spec.execute:
        ssh_config = (
            dict(config.get("ssh") or {})
            if backend == "cloud-ssh"
            else config
        )
        if not _IMAGE_DIGEST.fullmatch(
            str(ssh_config.get("remote_container_image") or "")
        ) or not re.fullmatch(
            r"[0-9a-fA-F]{64}",
            str(
                ssh_config.get("remote_container_runtime_sha256")
                or ""
            ),
        ):
            raise ComputeBindingError(
                "executable SSH jobs require a pinned remote container "
                "image and runtime sha256"
            )
    if (
        backend == "kubernetes"
        and canonical_spec.execute
        and (
            not config.get("source_pvc")
            or not config.get("artifact_pvc")
            or not _IMAGE_DIGEST.fullmatch(
                str(config.get("source_transport_image") or "")
            )
        )
    ):
        raise ComputeBindingError(
            "executable Kubernetes jobs require source_pvc, "
            "artifact_pvc, and a pinned source transport image"
        )
    if (
        backend == "slurm"
        and canonical_spec.execute
        and (
            not config.get("container_runtime")
            or not config.get("container_image")
        )
    ):
        raise ComputeBindingError(
            "executable Slurm jobs require a bound container runtime "
            "and immutable container image"
        )
    if backend in _REMOTE_EXECUTION_BACKENDS and canonical_spec.execute:
        supplied_source_hash = canonical_spec.metadata.get(
            "source_snapshot_sha256"
        )
        if supplied_source_hash not in (None, worktree_sha256):
            raise ComputeBindingError(
                "source_snapshot_sha256 does not match the approved worktree"
            )
        payload = canonical_spec.to_dict()
        source_metadata = {
            **dict(canonical_spec.metadata),
            "source_snapshot_sha256": worktree_sha256,
        }
        if backend in {"ssh", "cloud-ssh"}:
            legacy_source_hash = source_metadata.get(
                "remote_source_sha256"
            )
            if legacy_source_hash not in (None, worktree_sha256):
                raise ComputeBindingError(
                    "remote_source_sha256 does not match the approved worktree"
                )
            source_metadata["remote_source_sha256"] = worktree_sha256
        payload["metadata"] = source_metadata
        canonical_spec = JobSpec.from_dict(payload)
    if canonical_spec.execute and canonical_spec.job_id is None:
        payload = canonical_spec.to_dict()
        prefix = canonical_spec.name[:100].rstrip("._-") or "paperforge-job"
        payload["job_id"] = f"{prefix}-{canonical_spec.fingerprint[:16]}"
        canonical_spec = JobSpec.from_dict(payload)
    execution_worktree: str | None = None
    execution_snapshot_sha256: str | None = None
    if canonical_spec.execute:
        snapshot_key = hashlib.sha256(
            (
                worktree_sha256
                + canonical_spec.fingerprint
                + _canonical_json_sha256(config)
            ).encode("ascii")
        ).hexdigest()
        snapshot, execution_snapshot_sha256 = _materialize_local_snapshot(
            root,
            worktree,
            records=worktree_files,
            snapshot_key=snapshot_key,
            excluded_paths=mutable_outputs,
        )
        execution_worktree = str(snapshot)
    binding: dict[str, Any] = {
        "schema": COMPUTE_BINDING_SCHEMA,
        "backend": backend,
        "worktree": str(worktree),
        "job_spec": canonical_spec.to_dict(),
        "job_fingerprint": canonical_spec.fingerprint,
        "compute_config": config,
        "compute_config_sha256": _canonical_json_sha256(config),
        "compute_dependencies": config_dependencies,
        "executable": executable_record,
        "inputs": files,
        "inputs_sha256": _canonical_json_sha256({"files": files}),
        "worktree_files": worktree_files,
        "worktree_sha256": worktree_sha256,
        "mutable_outputs": sorted(str(path) for path in mutable_outputs),
        "execution_worktree": execution_worktree,
        "execution_snapshot_sha256": execution_snapshot_sha256,
        "git": _git_identity(worktree, root),
    }
    binding["binding_sha256"] = _canonical_json_sha256(binding)
    return canonical_spec, binding


def verify_compute_binding(
    workspace: str | Path,
    binding: Mapping[str, Any],
) -> tuple[bool, str]:
    """Recompute a persisted binding immediately before backend submission."""

    payload = dict(binding)
    expected = str(payload.pop("binding_sha256", ""))
    if (
        payload.get("schema") != COMPUTE_BINDING_SCHEMA
        or not expected
        or _canonical_json_sha256(payload) != expected
    ):
        return False, "compute binding checksum mismatch"
    raw_spec = payload.get("job_spec")
    raw_config = payload.get("compute_config")
    if not isinstance(raw_spec, Mapping) or not isinstance(raw_config, Mapping):
        return False, "compute binding payload is incomplete"
    try:
        _, current = build_compute_binding(
            workspace,
            job_spec=raw_spec,
            compute_backend=str(payload.get("backend", "")),
            compute_config=raw_config,
        )
    except (ComputeBindingError, OSError, ValueError, TypeError) as exc:
        return False, str(exc)
    if current.get("binding_sha256") != expected:
        return False, "compute executable, inputs, worktree, or Git identity changed"
    return True, "verified"


def job_manifest_inputs(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Convert a public job manifest into service inputs without hidden fields."""

    manifest = dict(payload)
    if manifest.get("schema") != COMPUTE_JOB_MANIFEST_SCHEMA:
        raise ComputeBindingError(
            f"job manifest schema must be {COMPUTE_JOB_MANIFEST_SCHEMA}"
        )
    allowed = {
        "schema",
        "compute_backend",
        "compute_config",
        "cost_limit",
        "experiment_stages",
        "job_spec",
    }
    unknown = sorted(set(manifest) - allowed)
    if unknown:
        raise ComputeBindingError(
            f"job manifest contains unsupported fields: {', '.join(unknown)}"
        )
    if not isinstance(manifest.get("job_spec"), Mapping):
        raise ComputeBindingError("job manifest requires a job_spec mapping")
    compute_config = manifest.get("compute_config") or {}
    if not isinstance(compute_config, Mapping):
        raise ComputeBindingError("job manifest compute_config must be a mapping")
    stage_payloads = manifest.get("experiment_stages") or {}
    if not isinstance(stage_payloads, Mapping):
        raise ComputeBindingError(
            "job manifest experiment_stages must be a mapping"
        )
    allowed_stages = {"static_check", "mini_experiment"}
    unknown_stages = sorted(set(stage_payloads) - allowed_stages)
    if unknown_stages:
        raise ComputeBindingError(
            "job manifest contains unsupported experiment stages: "
            + ", ".join(unknown_stages)
        )
    normalized_stages: dict[str, dict[str, Any]] = {}
    for stage, raw_stage in stage_payloads.items():
        if not isinstance(raw_stage, Mapping):
            raise ComputeBindingError(
                f"experiment stage {stage} must be a mapping"
            )
        allowed_stage_fields = {
            "compute_backend",
            "compute_config",
            "job_spec",
        }
        unknown_fields = sorted(set(raw_stage) - allowed_stage_fields)
        if unknown_fields:
            raise ComputeBindingError(
                f"experiment stage {stage} contains unsupported fields: "
                + ", ".join(unknown_fields)
            )
        raw_stage_spec = raw_stage.get("job_spec")
        raw_stage_config = raw_stage.get("compute_config") or {}
        if not isinstance(raw_stage_spec, Mapping) or not isinstance(
            raw_stage_config,
            Mapping,
        ):
            raise ComputeBindingError(
                f"experiment stage {stage} requires job_spec and mapping config"
            )
        normalized_stages[str(stage)] = {
            "compute_backend": str(
                raw_stage.get("compute_backend") or "local"
            ),
            "compute_config": dict(raw_stage_config),
            "job_spec": dict(raw_stage_spec),
        }
    return {
        "compute_backend": str(manifest.get("compute_backend") or "local"),
        "compute_config": dict(compute_config),
        "cost_limit": manifest.get("cost_limit"),
        "experiment_stages": normalized_stages,
        "job_spec": dict(manifest["job_spec"]),
    }


__all__ = [
    "COMPUTE_BINDING_SCHEMA",
    "COMPUTE_JOB_MANIFEST_SCHEMA",
    "ComputeBindingError",
    "build_compute_binding",
    "job_manifest_inputs",
    "verify_compute_binding",
]
