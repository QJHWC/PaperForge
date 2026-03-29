from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from engine.run_lock import run_lock


ROOT = Path(__file__).resolve().parent


def run_cmd(cmd: list[str], cwd: Path | None = None) -> None:
    print("[run]", " ".join(cmd))
    proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=False)
    if proc.returncode != 0:
        raise SystemExit(proc.returncode)


def _run_remote_cycle(remote_config_path: str, workspace: Path) -> Path:
    """SSH upload → remote train → download results. Returns local download dir."""
    from engine.remote_runner import RemoteRunner, load_remote_config

    cfg = load_remote_config(remote_config_path)
    download_dir = workspace / "remote_results"

    with RemoteRunner(cfg) as runner:
        exit_code = runner.run_full_cycle(str(download_dir))

    if exit_code != 0:
        print(f"[remote] WARNING: remote training exited with code {exit_code}")

    return download_dir


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Optionally run a server/cloud pipeline, then ingest outputs into PaperForge uploads."
    )
    p.add_argument("--workspace", required=True, help="Target PaperForge workspace directory.")
    p.add_argument("--remote-config", default=None, help="Path to remote.yaml for SSH-based remote execution.")
    p.add_argument("--cloud-run-dir", default=None, help="Directory containing server/cloud outputs.")
    p.add_argument("--pipeline-root", default=None, help="Optional pipeline root with scripts/run_all.py.")
    p.add_argument("--config", default=None, help="Optional config path relative to --pipeline-root.")
    p.add_argument("--run-name", default="final_full", help="Run name under results/runs when --cloud-run-dir is omitted.")
    p.add_argument("--mode", choices=["auto", "real", "sim"], default="auto")
    p.add_argument("--hardware-profile", default=None)
    p.add_argument("--device", default=None, help="Optional device override when external pipeline supports it.")
    p.add_argument("--skip-run", action="store_true", help="Skip optional pipeline execution and only ingest existing outputs.")
    p.add_argument("--skip-sync", action="store_true", help="Skip upload ingestion step.")
    return p


def main() -> None:
    args = build_parser().parse_args()

    workspace = Path(args.workspace).resolve()
    pipeline_root = (ROOT / args.pipeline_root).resolve() if args.pipeline_root else None
    config_path = (pipeline_root / args.config).resolve() if (pipeline_root and args.config) else None

    if not workspace.exists():
        raise FileNotFoundError(f"workspace not found: {workspace}")

    # ── Phase 1: Remote SSH execution (upload → train → download) ──
    remote_download_dir: Path | None = None
    if args.remote_config:
        with run_lock(workspace.resolve(), timeout=30, poll_interval=0.2, verbose=True):
            remote_download_dir = _run_remote_cycle(args.remote_config, workspace)
        if not args.cloud_run_dir:
            args.cloud_run_dir = str(remote_download_dir)

    # ── Phase 2: Optional local pipeline execution ──
    if not args.skip_run and pipeline_root is not None and (pipeline_root / "scripts" / "run_all.py").exists():
        if config_path is None or not config_path.exists():
            raise FileNotFoundError(f"config not found: {config_path}")
        run_all_cmd = [
            sys.executable,
            str(pipeline_root / "scripts" / "run_all.py"),
            "--config",
            str(config_path),
            "--mode",
            args.mode,
            "--run_name",
            args.run_name,
        ]
        if args.hardware_profile:
            run_all_cmd += ["--hardware_profile", args.hardware_profile]
        if args.device:
            run_all_cmd += ["--device", str(args.device)]
        run_cmd(run_all_cmd, cwd=pipeline_root)
    elif not args.skip_run and not args.remote_config:
        print("[warn] skip optional pipeline execution: missing --pipeline-root or scripts/run_all.py")

    # ── Phase 3: Determine cloud run directory ──
    if args.cloud_run_dir:
        cloud_run_dir = Path(args.cloud_run_dir).resolve()
    elif pipeline_root is not None:
        cloud_run_dir = (pipeline_root / "results" / "runs" / args.run_name).resolve()
    else:
        cloud_run_dir = None

    # ── Phase 4: Sync results into workspace uploads ──
    if not args.skip_sync:
        if cloud_run_dir is None:
            raise ValueError("cloud run directory is required: set --cloud-run-dir, --pipeline-root, or --remote-config")
        if not cloud_run_dir.exists():
            raise FileNotFoundError(f"cloud run dir not found: {cloud_run_dir}")
        sync_cmd = [
            sys.executable,
            str(ROOT / "sync_cloud_results_to_uploads.py"),
            "--workspace",
            str(workspace),
            "--cloud-run-dir",
            str(cloud_run_dir),
        ]
        if config_path is not None and config_path.exists():
            sync_cmd += ["--pipeline-config", str(config_path)]
        run_cmd(sync_cmd, cwd=ROOT)

    print("[done] cloud cycle complete")


if __name__ == "__main__":
    main()
