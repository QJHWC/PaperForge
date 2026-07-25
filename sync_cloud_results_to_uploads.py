from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

from engine.run_lock import run_lock

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


CODE_EXTENSIONS = {
    ".py",
    ".sh",
    ".json",
    ".csv",
    ".md",
    ".txt",
    ".tex",
    ".yaml",
    ".yml",
    ".log",
    ".npy",
    ".npz",
}
FIGURE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".svg", ".pdf"}
SKIP_DIR_NAMES = {
    "images",
    "labels",
    "dataset",
    "datasets",
    "train",
    "val",
    "test",
    "weights",
    "checkpoints",
    "__pycache__",
}
MAX_FILE_BYTES = 50 * 1024 * 1024
INCREMENTAL_STATE_VERSION = "1.1"
LEGACY_INCREMENTAL_STATE_VERSIONS = {None, 1, "1", "1.0"}
HASH_MODE_CHOICES = ("off", "auto", "full")
DEFAULT_INCREMENTAL_LARGE_FILE_BYTES = 50 * 1024 * 1024
DEFAULT_INCREMENTAL_HASH_SAMPLE_BYTES = 1024 * 1024

StateRecord = dict[str, object]
HashRecord = dict[str, str]


def _resolve(path: str) -> Path:
    p = Path(path)
    if p.is_absolute():
        return p
    return (Path.cwd() / p).resolve()


def _file_meta(path: Path) -> dict[str, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return {
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
    }


def _normalize_hash_record(raw: object) -> HashRecord | None:
    if not isinstance(raw, dict):
        return None
    mode = raw.get("mode")
    sha1 = raw.get("sha1")
    if mode not in {"full", "head_tail"}:
        return None
    if not isinstance(sha1, str) or not sha1.strip():
        return None
    return {"mode": mode, "sha1": sha1.strip()}


def _normalize_state_record(raw: object) -> StateRecord | None:
    if not isinstance(raw, dict):
        return None
    size = raw.get("size")
    mtime_ns = raw.get("mtime_ns")
    if not isinstance(size, int) or not isinstance(mtime_ns, int):
        return None
    record: StateRecord = {"size": size, "mtime_ns": mtime_ns}
    hash_record = _normalize_hash_record(raw.get("hash"))
    if hash_record is not None:
        record["hash"] = hash_record
    return record


def _meta_equal(left: StateRecord | None, right: dict[str, int] | None) -> bool:
    if left is None or right is None:
        return False
    return left.get("size") == right.get("size") and left.get("mtime_ns") == right.get("mtime_ns")


def _hash_records_equal(left: HashRecord | None, right: HashRecord | None) -> bool:
    if left is None or right is None:
        return False
    return left.get("mode") == right.get("mode") and left.get("sha1") == right.get("sha1")


def _compute_full_sha1(path: Path) -> str:
    hasher = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _compute_head_tail_sha1(path: Path, size: int, sample_bytes: int) -> HashRecord:
    if size <= sample_bytes * 2:
        return {"mode": "full", "sha1": _compute_full_sha1(path)}

    hasher = hashlib.sha1()
    with path.open("rb") as f:
        head = f.read(sample_bytes)
        f.seek(max(0, size - sample_bytes))
        tail = f.read(sample_bytes)
    hasher.update(head)
    hasher.update(tail)
    return {"mode": "head_tail", "sha1": hasher.hexdigest()}


def _compute_hash_record(
    path: Path,
    meta: dict[str, int],
    hash_mode: str,
    large_file_bytes: int,
    sample_bytes: int,
    cache: dict[tuple[str, str, int, int, int, int], HashRecord],
) -> HashRecord:
    cache_key = (
        str(path),
        hash_mode,
        meta["size"],
        meta["mtime_ns"],
        large_file_bytes,
        sample_bytes,
    )
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    if hash_mode == "full":
        record = {"mode": "full", "sha1": _compute_full_sha1(path)}
    elif hash_mode == "auto":
        if meta["size"] <= large_file_bytes:
            record = {"mode": "full", "sha1": _compute_full_sha1(path)}
        else:
            record = _compute_head_tail_sha1(path, meta["size"], sample_bytes)
    else:
        raise ValueError(f"Unsupported hash mode: {hash_mode}")

    cache[cache_key] = record
    return record


def _state_record_from_meta(meta: dict[str, int], trusted_hash: HashRecord | None) -> StateRecord:
    record: StateRecord = {"size": meta["size"], "mtime_ns": meta["mtime_ns"]}
    if trusted_hash is not None:
        record["hash"] = trusted_hash
    return record


def _decide_incremental_copy(
    src: Path,
    dst: Path,
    current_meta: dict[str, int],
    previous_record: StateRecord | None,
    hash_mode: str,
    large_file_bytes: int,
    sample_bytes: int,
    hash_cache: dict[tuple[str, str, int, int, int, int], HashRecord],
) -> tuple[bool, HashRecord | None, str]:
    if previous_record is None:
        return True, None, "new_file"

    prev_size = int(previous_record["size"])
    prev_mtime = int(previous_record["mtime_ns"])
    prev_hash = _normalize_hash_record(previous_record.get("hash"))

    # A: size changed -> definitely modified.
    if current_meta["size"] != prev_size:
        return True, None, "size_changed"

    # B: size + mtime unchanged -> fast skip.
    if current_meta["mtime_ns"] == prev_mtime:
        return False, prev_hash, "size_mtime_unchanged"

    # C: size unchanged + mtime changed -> hash check if enabled.
    if hash_mode == "off":
        return True, None, "mtime_changed_hash_disabled"

    src_hash = _compute_hash_record(
        path=src,
        meta=current_meta,
        hash_mode=hash_mode,
        large_file_bytes=large_file_bytes,
        sample_bytes=sample_bytes,
        cache=hash_cache,
    )
    if _hash_records_equal(src_hash, prev_hash):
        return False, src_hash, "mtime_changed_hash_equal_prev_state"

    # State migration fallback: compare with existing destination hash when old state has no hash.
    if prev_hash is None and dst.exists():
        dst_meta = _file_meta(dst)
        if dst_meta is not None and dst_meta["size"] == current_meta["size"]:
            dst_hash = _compute_hash_record(
                path=dst,
                meta=dst_meta,
                hash_mode=hash_mode,
                large_file_bytes=large_file_bytes,
                sample_bytes=sample_bytes,
                cache=hash_cache,
            )
            if _hash_records_equal(src_hash, dst_hash):
                return False, src_hash, "mtime_changed_hash_equal_destination"

    return True, src_hash, "mtime_changed_hash_diff"


def _atomic_write_text(path: Path, content: str) -> None:
    tmp_path = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def _load_incremental_state(
    state_file: Path,
    cloud_run_dir: Path,
) -> tuple[dict[str, StateRecord], StateRecord | None, bool]:
    if not state_file.exists():
        return {}, None, False
    try:
        payload = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[sync][incremental] ignored invalid state file `{state_file}`: {exc}")
        return {}, None, False

    version = payload.get("version")
    is_legacy_version = version in LEGACY_INCREMENTAL_STATE_VERSIONS
    if not is_legacy_version and str(version) != INCREMENTAL_STATE_VERSION:
        print("[sync][incremental] state version mismatch, baseline ignored.")
        return {}, None, False

    state_cloud_dir = str(payload.get("cloud_run_dir", "")).strip()
    if state_cloud_dir != str(cloud_run_dir):
        print("[sync][incremental] cloud_run_dir changed, baseline ignored.")
        return {}, None, False

    files = payload.get("files")
    if not isinstance(files, dict):
        return {}, None, False

    normalized_files: dict[str, StateRecord] = {}
    for key, value in files.items():
        if not isinstance(key, str):
            continue
        record = _normalize_state_record(value)
        if record is not None:
            normalized_files[key] = record

    pipeline_meta = payload.get("pipeline_config")
    normalized_pipeline_meta = _normalize_state_record(pipeline_meta)

    return normalized_files, normalized_pipeline_meta, is_legacy_version


def _save_incremental_state(
    state_file: Path,
    cloud_run_dir: Path,
    files: dict[str, StateRecord],
    pipeline_meta: StateRecord | None,
) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": INCREMENTAL_STATE_VERSION,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "cloud_run_dir": str(cloud_run_dir),
        "files": files,
        "pipeline_config": pipeline_meta,
    }
    _atomic_write_text(state_file, json.dumps(payload, ensure_ascii=False, indent=2))


def _collect_generic_artifacts(
    base_dir: Path,
    pipeline_config: Path | None = None,
) -> tuple[list[tuple[Path, Path]], list[tuple[Path, Path]], dict[str, dict[str, int]], dict[str, int] | None]:
    code_files: list[tuple[Path, Path]] = []
    figure_files: list[tuple[Path, Path]] = []
    current_files: dict[str, dict[str, int]] = {}
    pipeline_meta: dict[str, int] | None = None

    # Use os.walk so we can prune large irrelevant subtrees early.
    for root, dirnames, filenames in os.walk(base_dir):
        dirnames[:] = [name for name in dirnames if name.lower() not in SKIP_DIR_NAMES]
        root_path = Path(root)
        for filename in filenames:
            path = root_path / filename
            meta = _file_meta(path)
            if meta is None:
                continue
            if meta["size"] > MAX_FILE_BYTES:
                continue

            rel_path = path.relative_to(base_dir)
            rel_key = rel_path.as_posix()
            current_files[rel_key] = meta
            suffix = path.suffix.lower()
            if suffix in FIGURE_EXTENSIONS:
                figure_files.append((path, rel_path))
            elif suffix in CODE_EXTENSIONS:
                code_files.append((path, rel_path))

    if pipeline_config is not None and pipeline_config.exists():
        pipeline_meta = _file_meta(pipeline_config)

    return code_files, figure_files, current_files, pipeline_meta


def _copy_file(src: Path, dst: Path, overwrite: bool) -> bool:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and not overwrite:
        return False
    shutil.copy2(src, dst)
    return True


def _append_ingest_note(notes_md: Path, cloud_run_dir: Path, copied_code: int, copied_figs: int) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"- [{now}] Ingested cloud run: `{cloud_run_dir}`",
        f"  - code files copied: {copied_code}",
        f"  - figure files copied: {copied_figs}",
    ]
    if notes_md.exists():
        original = notes_md.read_text(encoding="utf-8").rstrip()
        notes_md.write_text(original + "\n" + "\n".join(lines) + "\n", encoding="utf-8")
    else:
        notes_md.write_text("# User Upload Notes\n\n" + "\n".join(lines) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Copy server/cloud outputs into MVP uploads/, then refresh workflow artifacts."
    )
    p.add_argument("--workspace", required=True, help="PaperForge run workspace (contains uploads/ and notes.txt).")
    p.add_argument("--cloud-run-dir", required=True, help="Cloud/server output directory to ingest.")
    p.add_argument("--pipeline-config", default=None, help="Optional pipeline config yaml to copy as cloud_config.yaml.")
    p.add_argument("--overwrite", action="store_true", default=True, help="Overwrite same-name files in uploads (default: true).")
    p.add_argument("--no-overwrite", action="store_false", dest="overwrite")
    p.add_argument(
        "--incremental",
        action="store_true",
        help="Only copy new/changed files by comparing size+mtime against previous sync state.",
    )
    p.add_argument(
        "--incremental-state-file",
        default=None,
        help="Optional state json path for incremental mode (default: <workspace>/artifacts/cloud_sync_incremental_state.json).",
    )
    p.add_argument(
        "--incremental-hash-mode",
        choices=HASH_MODE_CHOICES,
        default="auto",
        help=(
            "Hash strategy for incremental mode when size is unchanged but mtime differs: "
            "off=mtime-only, auto=small file full SHA1 + large file head/tail SHA1, full=always full SHA1."
        ),
    )
    p.add_argument(
        "--incremental-large-file-bytes",
        type=int,
        default=DEFAULT_INCREMENTAL_LARGE_FILE_BYTES,
        help="Large-file threshold used by --incremental-hash-mode auto (default: 50MB).",
    )
    p.add_argument(
        "--incremental-hash-sample-bytes",
        type=int,
        default=DEFAULT_INCREMENTAL_HASH_SAMPLE_BYTES,
        help="Head/tail sample size used by incremental hash auto mode (default: 1MB).",
    )
    p.add_argument("--dry-run", action="store_true", help="Preview files only.")
    return p


def main() -> None:
    args = build_parser().parse_args()

    from engine.mvp_workflow import (
        append_upload_feedback_to_notes,
        ensure_upload_interface,
        ingest_user_uploads,
        load_workflow_state,
        save_workflow_state,
    )

    workspace = _resolve(args.workspace)
    cloud_run_dir = _resolve(args.cloud_run_dir)
    pipeline_config = _resolve(args.pipeline_config) if args.pipeline_config else None

    if not workspace.exists():
        raise FileNotFoundError(f"workspace not found: {workspace}")
    if not cloud_run_dir.exists():
        raise FileNotFoundError(f"cloud run dir not found: {cloud_run_dir}")
    if args.incremental_large_file_bytes <= 0:
        raise ValueError("--incremental-large-file-bytes must be > 0")
    if args.incremental_hash_sample_bytes <= 0:
        raise ValueError("--incremental-hash-sample-bytes must be > 0")

    with run_lock(workspace.resolve(), timeout=30, poll_interval=0.2, verbose=True):
        upload_paths = ensure_upload_interface(str(workspace))
        code_dir = Path(upload_paths["code_dir"])
        fig_dir = Path(upload_paths["figures_dir"])
        notes_md = Path(upload_paths["notes_md"])

        if args.incremental and args.incremental_state_file:
            state_file = _resolve(args.incremental_state_file)
        else:
            state_file = workspace / "artifacts" / "cloud_sync_incremental_state.json"

        previous_files: dict[str, StateRecord] = {}
        previous_pipeline_meta: StateRecord | None = None
        loaded_legacy_state = False
        if args.incremental:
            previous_files, previous_pipeline_meta, loaded_legacy_state = _load_incremental_state(
                state_file=state_file,
                cloud_run_dir=cloud_run_dir,
            )

        code_files, fig_files, current_files, current_pipeline_meta = _collect_generic_artifacts(
            cloud_run_dir,
            pipeline_config=pipeline_config,
        )

        code_copied = 0
        fig_copied = 0
        hash_cache: dict[tuple[str, str, int, int, int, int], HashRecord] = {}
        current_state_files: dict[str, StateRecord] = {}
        current_pipeline_record: StateRecord | None = None

        print(f"[sync] workspace: {workspace}")
        print(f"[sync] cloud run dir: {cloud_run_dir}")
        if args.incremental:
            print(f"[sync][incremental] state file: {state_file}")
            print(f"[sync][incremental] baseline files: {len(previous_files)}")
            if loaded_legacy_state:
                print("[sync][incremental] loaded legacy state; hash fields will be backfilled gradually.")
            print(
                "[sync][incremental] hash mode: "
                f"{args.incremental_hash_mode} "
                f"(large_file_bytes={args.incremental_large_file_bytes}, sample_bytes={args.incremental_hash_sample_bytes})"
            )
        print(f"[sync] code discovered: {len(code_files)}")
        print(f"[sync] figure discovered: {len(fig_files)}")

        for src, rel_path in code_files:
            dst = code_dir / rel_path
            rel_key = rel_path.as_posix()
            meta = current_files[rel_key]

            should_copy = True
            trusted_hash = None
            reason = "full_sync"
            if args.incremental:
                should_copy, trusted_hash, reason = _decide_incremental_copy(
                    src=src,
                    dst=dst,
                    current_meta=meta,
                    previous_record=previous_files.get(rel_key),
                    hash_mode=args.incremental_hash_mode,
                    large_file_bytes=args.incremental_large_file_bytes,
                    sample_bytes=args.incremental_hash_sample_bytes,
                    hash_cache=hash_cache,
                )

            current_state_files[rel_key] = _state_record_from_meta(meta, trusted_hash)
            if not should_copy:
                if args.dry_run:
                    print(f"[dry-run][skip][code] {src} ({reason})")
                continue

            if args.dry_run:
                print(f"[dry-run][code] {src} -> {dst} ({reason})")
                continue
            if _copy_file(src, dst, overwrite=args.overwrite):
                code_copied += 1
                print(f"[copy][code] {rel_path} ({reason})")

        for src, rel_path in fig_files:
            dst = fig_dir / rel_path
            rel_key = rel_path.as_posix()
            meta = current_files[rel_key]

            should_copy = True
            trusted_hash = None
            reason = "full_sync"
            if args.incremental:
                should_copy, trusted_hash, reason = _decide_incremental_copy(
                    src=src,
                    dst=dst,
                    current_meta=meta,
                    previous_record=previous_files.get(rel_key),
                    hash_mode=args.incremental_hash_mode,
                    large_file_bytes=args.incremental_large_file_bytes,
                    sample_bytes=args.incremental_hash_sample_bytes,
                    hash_cache=hash_cache,
                )

            current_state_files[rel_key] = _state_record_from_meta(meta, trusted_hash)
            if not should_copy:
                if args.dry_run:
                    print(f"[dry-run][skip][fig] {src} ({reason})")
                continue

            if args.dry_run:
                print(f"[dry-run][fig] {src} -> {dst} ({reason})")
                continue
            if _copy_file(src, dst, overwrite=args.overwrite):
                fig_copied += 1
                print(f"[copy][fig] {rel_path} ({reason})")

        if pipeline_config and pipeline_config.exists() and current_pipeline_meta is not None:
            pipeline_dst = code_dir / "cloud_config.yaml"
            should_copy_pipeline = True
            pipeline_hash = None
            pipeline_reason = "pipeline_present"
            if args.incremental:
                should_copy_pipeline, pipeline_hash, pipeline_reason = _decide_incremental_copy(
                    src=pipeline_config,
                    dst=pipeline_dst,
                    current_meta=current_pipeline_meta,
                    previous_record=previous_pipeline_meta,
                    hash_mode=args.incremental_hash_mode,
                    large_file_bytes=args.incremental_large_file_bytes,
                    sample_bytes=args.incremental_hash_sample_bytes,
                    hash_cache=hash_cache,
                )
            current_pipeline_record = _state_record_from_meta(current_pipeline_meta, pipeline_hash)

            if should_copy_pipeline:
                if args.dry_run:
                    print(f"[dry-run][code] {pipeline_config} -> {pipeline_dst} ({pipeline_reason})")
                elif _copy_file(pipeline_config, pipeline_dst, overwrite=args.overwrite):
                    code_copied += 1
                    print(f"[copy][code] cloud_config.yaml ({pipeline_reason})")
            elif args.dry_run:
                print(f"[dry-run][skip][code] {pipeline_config} ({pipeline_reason})")

        if args.dry_run:
            return

        _append_ingest_note(notes_md, cloud_run_dir=cloud_run_dir, copied_code=code_copied, copied_figs=fig_copied)

        manifest = ingest_user_uploads(str(workspace))
        notes_path = workspace / "notes.txt"
        if notes_path.exists():
            append_upload_feedback_to_notes(str(notes_path), manifest)

        state = load_workflow_state(str(workspace))
        state.update(
            {
                "phase": "cloud_results_ingested",
                "cloud_run_dir": str(cloud_run_dir),
                "upload_manifest": str(workspace / "artifacts" / "upload_manifest.json"),
            }
        )
        save_workflow_state(str(workspace), state)

        if args.incremental:
            _save_incremental_state(
                state_file=state_file,
                cloud_run_dir=cloud_run_dir,
                files=current_state_files,
                pipeline_meta=current_pipeline_record,
            )
            print(f"[sync][incremental] state updated: {state_file}")

        print(f"[sync] copied code files: {code_copied}")
        print(f"[sync] copied figure files: {fig_copied}")
        print(f"[sync] manifest: {workspace / 'artifacts' / 'upload_manifest.json'}")


if __name__ == "__main__":
    main()
