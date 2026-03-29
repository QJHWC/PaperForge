"""Pre-flight validation for PaperForge workspaces and remote environments.

Run before training to catch missing files, wrong configs, or environment
issues *before* wasting GPU hours.

Usage:
    python -m engine.preflight --workspace ./workspace/results/my_exp
    python -m engine.preflight --workspace ./workspace/results/my_exp --remote-config remote.yaml
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml


@classmethod
def _noop(*a, **kw):
    pass


class PreflightResult:
    def __init__(self) -> None:
        self.passed: List[str] = []
        self.failed: List[str] = []
        self.warnings: List[str] = []

    def ok(self, msg: str) -> None:
        self.passed.append(msg)

    def fail(self, msg: str) -> None:
        self.failed.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    @property
    def success(self) -> bool:
        return len(self.failed) == 0

    def summary(self) -> str:
        lines = []
        for m in self.passed:
            lines.append(f"  [PASS] {m}")
        for m in self.warnings:
            lines.append(f"  [WARN] {m}")
        for m in self.failed:
            lines.append(f"  [FAIL] {m}")
        lines.append(
            f"\n  Result: {len(self.passed)} passed, "
            f"{len(self.failed)} failed, {len(self.warnings)} warnings"
        )
        return "\n".join(lines)


def check_files_exist(paths: List[str | Path], base_dir: Path | None = None) -> PreflightResult:
    """Verify that all listed files exist."""
    result = PreflightResult()
    for p in paths:
        fp = Path(p) if base_dir is None else base_dir / p
        if fp.exists():
            result.ok(f"{fp}")
        else:
            result.fail(f"{fp} not found")
    return result


def check_executables(names: List[str]) -> PreflightResult:
    """Verify that required executables are on PATH."""
    result = PreflightResult()
    for name in names:
        if shutil.which(name):
            result.ok(f"{name} found")
        else:
            result.fail(f"{name} not on PATH")
    return result


def check_python_environment() -> PreflightResult:
    """Check Python version, virtualenv, and key packages."""
    result = PreflightResult()

    result.ok(f"Python {sys.version.split()[0]}")

    in_venv = getattr(sys, "base_prefix", sys.prefix) != sys.prefix
    if in_venv:
        result.ok("virtualenv active")
    else:
        result.warn("not in a virtualenv (set PAPERFORGE_ALLOW_SYSTEM_PYTHON=1 to suppress)")

    for pkg in ("yaml", "numpy"):
        try:
            __import__(pkg)
            result.ok(f"import {pkg}")
        except ImportError:
            result.fail(f"cannot import {pkg}")

    return result


def check_gpu() -> PreflightResult:
    """Detect GPU availability via torch (optional)."""
    result = PreflightResult()
    try:
        import torch
        if torch.cuda.is_available():
            for i in range(torch.cuda.device_count()):
                result.ok(f"GPU {i}: {torch.cuda.get_device_name(i)}")
        else:
            result.warn("CUDA not available (CPU-only mode)")
    except ImportError:
        result.warn("torch not installed (GPU detection skipped)")
    return result


def check_yaml_config(
    config_path: Path,
    required_keys: Optional[List[str]] = None,
    expected_values: Optional[Dict[str, object]] = None,
) -> PreflightResult:
    """Validate a YAML config file structure and optional values."""
    result = PreflightResult()
    if not config_path.exists():
        result.fail(f"config not found: {config_path}")
        return result

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception as e:
        result.fail(f"YAML parse error: {e}")
        return result

    result.ok(f"config loaded: {config_path}")

    if required_keys:
        for key in required_keys:
            parts = key.split(".")
            obj = cfg
            found = True
            for part in parts:
                if isinstance(obj, dict) and part in obj:
                    obj = obj[part]
                else:
                    found = False
                    break
            if found:
                result.ok(f"key '{key}' present")
            else:
                result.fail(f"key '{key}' missing")

    if expected_values:
        for key, expected in expected_values.items():
            parts = key.split(".")
            obj = cfg
            found = True
            for part in parts:
                if isinstance(obj, dict) and part in obj:
                    obj = obj[part]
                else:
                    found = False
                    break
            if not found:
                result.fail(f"key '{key}' missing (expected {expected})")
            elif str(obj) == str(expected):
                result.ok(f"{key} = {obj}")
            else:
                result.fail(f"{key} = {obj} (expected {expected})")

    return result


def check_remote_config(config_path: Path) -> PreflightResult:
    """Validate a remote.yaml SSH config."""
    result = PreflightResult()
    if not config_path.exists():
        result.fail(f"remote config not found: {config_path}")
        return result

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception as e:
        result.fail(f"YAML parse error: {e}")
        return result

    for key in ("host", "train_command", "results_dir"):
        val = cfg.get(key, "")
        if val:
            result.ok(f"{key} = {val}")
        else:
            result.fail(f"{key} is empty or missing")

    auth = cfg.get("auth", {})
    method = auth.get("method", "key")
    if method == "key":
        key_path = os.path.expanduser(auth.get("key_path", "~/.ssh/id_rsa"))
        if os.path.exists(key_path):
            result.ok(f"SSH key exists: {key_path}")
        else:
            result.fail(f"SSH key not found: {key_path}")
    elif method == "password":
        pw = auth.get("password", "")
        if pw:
            result.ok("password configured")
        else:
            result.fail("auth.method=password but no password set")
    else:
        result.fail(f"unknown auth method: {method}")

    return result


def run_all_checks(
    workspace: Optional[Path] = None,
    required_files: Optional[List[str]] = None,
    remote_config: Optional[Path] = None,
    config_path: Optional[Path] = None,
    config_required_keys: Optional[List[str]] = None,
    config_expected_values: Optional[Dict[str, object]] = None,
) -> PreflightResult:
    """Run a comprehensive pre-flight check suite."""
    combined = PreflightResult()

    def _merge(r: PreflightResult) -> None:
        combined.passed.extend(r.passed)
        combined.failed.extend(r.failed)
        combined.warnings.extend(r.warnings)

    _merge(check_python_environment())
    _merge(check_executables(["pdflatex", "bibtex"]))
    _merge(check_gpu())

    if required_files:
        base = workspace or Path.cwd()
        _merge(check_files_exist(required_files, base_dir=base))

    if workspace and workspace.exists():
        combined.ok(f"workspace exists: {workspace}")
    elif workspace:
        combined.fail(f"workspace not found: {workspace}")

    if config_path:
        _merge(check_yaml_config(config_path, config_required_keys, config_expected_values))

    if remote_config:
        _merge(check_remote_config(remote_config))

    return combined


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="PaperForge pre-flight checks")
    parser.add_argument("--workspace", default=None, help="Workspace directory to validate")
    parser.add_argument("--remote-config", default=None, help="Path to remote.yaml")
    parser.add_argument("--config", default=None, help="Path to experiment YAML config")
    parser.add_argument("--require-files", nargs="*", default=None, help="Files that must exist")
    args = parser.parse_args()

    result = run_all_checks(
        workspace=Path(args.workspace) if args.workspace else None,
        required_files=args.require_files,
        remote_config=Path(args.remote_config) if args.remote_config else None,
        config_path=Path(args.config) if args.config else None,
    )

    print("\n=== PaperForge Pre-flight Check ===\n")
    print(result.summary())
    print()

    raise SystemExit(0 if result.success else 1)


if __name__ == "__main__":
    main()
