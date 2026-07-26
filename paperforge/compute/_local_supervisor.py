from __future__ import annotations

import json
import os
import queue
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from engine.secret_redaction import redact_secrets

_PRIVATE_KEY_BEGIN = re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----")
_PRIVATE_KEY_END = re.compile(r"-----END [^-]*PRIVATE KEY-----")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary = handle.name
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def _kill_process_group(pid: int, sig: int) -> None:
    killpg = getattr(os, "killpg", None)
    if killpg is None:
        raise AttributeError("process-group signaling is unavailable")
    killpg(pid, sig)


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            os.kill(
                process.pid,
                getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM),
            )
        except (OSError, ProcessLookupError):
            process.terminate()
    else:
        try:
            _kill_process_group(process.pid, signal.SIGTERM)
        except (AttributeError, ProcessLookupError):
            process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            process.kill()
        else:
            try:
                _kill_process_group(
                    process.pid,
                    getattr(signal, "SIGKILL", signal.SIGTERM),
                )
            except (AttributeError, ProcessLookupError):
                process.kill()
        process.wait(timeout=5)


def supervise(launch_path: Path) -> int:
    launch = json.loads(launch_path.read_text(encoding="utf-8"))
    command = [str(part) for part in launch["command"]]
    cwd = Path(str(launch["cwd"])).resolve(strict=True)
    environment = {
        str(key): str(value) for key, value in dict(launch["environment"]).items()
    }
    log_path = Path(str(launch["log_path"]))
    completion_path = Path(str(launch["completion_path"]))
    identity_path = Path(str(launch["identity_path"]))
    timeout_seconds = launch.get("timeout_seconds")
    append = bool(launch.get("append", False))
    cancelled = False
    process: subprocess.Popen[bytes] | None = None

    def request_cancel(_signum: int, _frame: Any) -> None:
        nonlocal cancelled
        cancelled = True
        if process is not None:
            _terminate(process)

    signal.signal(signal.SIGTERM, request_cancel)
    signal.signal(signal.SIGINT, request_cancel)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, request_cancel)
    _atomic_json(
        identity_path,
        {
            "schema": "paperforge.local-supervisor/v1",
            "pid": os.getpid(),
            "nonce": str(launch["nonce"]),
            "launch_path": str(launch_path),
            "started_at": time.time(),
        },
    )
    mode = "ab" if append else "wb"
    started = time.monotonic()
    private_key = False
    return_code: int | None = None
    status = "FAILED"
    message = "local supervisor failed before process completion"
    try:
        process_options: dict[str, Any] = {
            "cwd": cwd,
            "env": environment,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
        }
        if os.name == "nt":
            process_options["creationflags"] = getattr(
                subprocess,
                "CREATE_NEW_PROCESS_GROUP",
                0x00000200,
            )
        else:
            process_options["start_new_session"] = True
        process = subprocess.Popen(command, **process_options)
        assert process.stdout is not None
        output_queue: queue.Queue[bytes | None] = queue.Queue()

        def read_output() -> None:
            assert process is not None and process.stdout is not None
            try:
                while True:
                    line = process.stdout.readline()
                    if not line:
                        break
                    output_queue.put(line)
            finally:
                output_queue.put(None)

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()

        def write_line(raw_line: bytes, log_handle: Any) -> None:
            nonlocal private_key
            line = raw_line.decode("utf-8", errors="replace")
            begins_private_key = bool(_PRIVATE_KEY_BEGIN.search(line))
            ends_private_key = bool(_PRIVATE_KEY_END.search(line))
            if private_key or begins_private_key:
                if begins_private_key and not private_key:
                    log_handle.write(b"***redacted***\n")
                    log_handle.flush()
                private_key = not ends_private_key
                return
            log_handle.write(redact_secrets(line).encode("utf-8"))
            log_handle.flush()

        with log_path.open(mode) as log_handle:
            output_closed = False
            while True:
                if (
                    timeout_seconds is not None
                    and time.monotonic() - started > float(timeout_seconds)
                ):
                    _terminate(process)
                    return_code = process.returncode
                    status = "FAILED"
                    message = "local process exceeded its timeout"
                    break
                try:
                    raw_line = output_queue.get(timeout=0.1)
                except queue.Empty:
                    raw_line = b""
                if raw_line is None:
                    output_closed = True
                elif raw_line:
                    write_line(raw_line, log_handle)
                return_code = process.poll()
                if return_code is not None:
                    while not output_closed:
                        try:
                            remaining = output_queue.get(timeout=0.1)
                        except queue.Empty:
                            if not reader.is_alive():
                                output_closed = True
                            continue
                        if remaining is None:
                            output_closed = True
                        else:
                            write_line(remaining, log_handle)
                    if cancelled:
                        status = "CANCELLED"
                        message = "local process was cancelled"
                    elif return_code == 0:
                        status = "SUCCEEDED"
                        message = "local process completed"
                    else:
                        status = "FAILED"
                        message = f"local process exited with code {return_code}"
                    break
                time.sleep(0.02)
    except BaseException as exc:
        if process is not None:
            _terminate(process)
            return_code = process.returncode
        status = "CANCELLED" if cancelled else "FAILED"
        message = (
            "local process was cancelled"
            if cancelled
            else f"local supervisor error: {type(exc).__name__}"
        )
    finally:
        _atomic_json(
            completion_path,
            {
                "schema": "paperforge.local-completion/v1",
                "status": status,
                "return_code": return_code,
                "message": message,
                "completed_at": time.time(),
                "nonce": str(launch["nonce"]),
            },
        )
    return 0 if status == "SUCCEEDED" else 1


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1:
        return 2
    return supervise(Path(arguments[0]).expanduser().resolve(strict=True))


if __name__ == "__main__":
    raise SystemExit(main())
