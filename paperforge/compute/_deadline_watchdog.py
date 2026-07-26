from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from paperforge.path_safety import (
    is_link_or_reparse_point,
    reject_symlink_components,
)

_CONTAINER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _canonical_sha256(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary = handle.name
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def _load(path: Path) -> dict[str, Any]:
    lexical = path.expanduser().absolute()
    reject_symlink_components(lexical, anchor=Path(lexical.anchor))
    if is_link_or_reparse_point(lexical) or not lexical.is_file():
        raise ValueError("watchdog configuration must be a regular file")
    payload = json.loads(lexical.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("watchdog configuration must be an object")
    expected = str(payload.pop("config_sha256", ""))
    if not _SHA256.fullmatch(expected) or _canonical_sha256(payload) != expected:
        raise ValueError("watchdog configuration checksum mismatch")
    return payload


def run(config_path: str | Path) -> int:
    payload = _load(Path(config_path))
    runtime = Path(str(payload["runtime"]))
    name = str(payload["container_name"])
    container_id = str(payload["container_id"])
    deadline = float(payload["deadline_epoch"])
    marker = Path(str(payload["timeout_marker"])).absolute()
    identity = str(payload["identity_sha256"])
    if (
        not runtime.is_absolute()
        or is_link_or_reparse_point(runtime)
        or not runtime.is_file()
        or not os.access(runtime, os.X_OK)
        or not _CONTAINER_NAME.fullmatch(name)
        or not _SHA256.fullmatch(container_id)
        or not _SHA256.fullmatch(identity)
        or marker.parent != Path(config_path).expanduser().absolute().parent
    ):
        raise ValueError("watchdog configuration contains an unsafe value")
    reject_symlink_components(marker, anchor=marker.parent)
    delay = deadline - time.time()
    if delay > 0:
        time.sleep(delay)
    inspected = subprocess.run(
        [
            str(runtime),
            "inspect",
            "--format",
            "{{.Id}}|{{.State.Running}}",
            name,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    raw_id, _, running = inspected.stdout.strip().partition("|")
    if inspected.returncode != 0 or raw_id != container_id or running != "true":
        return 0
    stopped = subprocess.run(
        [str(runtime), "rm", "--force", name],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if stopped.returncode != 0:
        return 1
    verified = subprocess.run(
        [str(runtime), "inspect", "--format", "{{.Id}}", name],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if verified.returncode == 0:
        return 1
    _atomic_json(
        marker,
        {
            "schema": "paperforge.compute-timeout/v1",
            "container_id": container_id,
            "container_name": name,
            "deadline_epoch": deadline,
            "identity_sha256": identity,
            "status": "TIMED_OUT",
        },
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1:
        raise SystemExit("usage: python -m paperforge.compute._deadline_watchdog CONFIG")
    return run(arguments[0])


if __name__ == "__main__":
    raise SystemExit(main())
