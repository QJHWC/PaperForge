from __future__ import annotations

import ctypes
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any


def _checked_run(argv: list[str]) -> None:
    completed = subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError("Windows sandbox ACL operation failed")


def _grant(path: Path, sid: str, rights: str) -> None:
    _checked_run(["icacls", str(path), "/grant", f"*{sid}:{rights}"])


def _remove_grant(path: Path, sid: str) -> None:
    subprocess.run(
        ["icacls", str(path), "/remove:g", f"*{sid}"],
        check=False,
        capture_output=True,
        text=True,
    )


def _run_appcontainer(config: dict[str, Any]) -> int:
    if os.name != "nt":
        raise RuntimeError("AppContainer execution requires Windows")
    from ctypes import wintypes

    win_dll = ctypes.__dict__["WinDLL"]
    win_error = ctypes.__dict__["WinError"]
    get_last_error = ctypes.__dict__["get_last_error"]
    kernel32 = win_dll("kernel32", use_last_error=True)
    userenv = win_dll("userenv", use_last_error=True)
    advapi32 = win_dll("advapi32", use_last_error=True)

    class SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    class SECURITY_CAPABILITIES(ctypes.Structure):
        _fields_ = [
            ("AppContainerSid", ctypes.c_void_p),
            ("Capabilities", ctypes.POINTER(SID_AND_ATTRIBUTES)),
            ("CapabilityCount", wintypes.DWORD),
            ("Reserved", wintypes.DWORD),
        ]

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.POINTER(ctypes.c_ubyte)),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class STARTUPINFOEXW(ctypes.Structure):
        _fields_ = [
            ("StartupInfo", STARTUPINFOW),
            ("lpAttributeList", ctypes.c_void_p),
        ]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    class SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", ctypes.c_void_p),
            ("bInheritHandle", wintypes.BOOL),
        ]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    kernel32.InitializeProcThreadAttributeList.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
    kernel32.UpdateProcThreadAttribute.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL
    kernel32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel32.GetStdHandle.restype = wintypes.HANDLE
    kernel32.DuplicateHandle.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    kernel32.DuplicateHandle.restype = wintypes.BOOL
    kernel32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(STARTUPINFOEXW),
        ctypes.POINTER(PROCESS_INFORMATION),
    ]
    kernel32.CreateProcessW.restype = wintypes.BOOL
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
    ]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    create_profile = userenv.CreateAppContainerProfile
    create_profile.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        ctypes.POINTER(SID_AND_ATTRIBUTES),
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    create_profile.restype = ctypes.c_long
    derive_sid = userenv.DeriveAppContainerSidFromAppContainerName
    derive_sid.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    derive_sid.restype = ctypes.c_long
    delete_profile = userenv.DeleteAppContainerProfile
    delete_profile.argtypes = [wintypes.LPCWSTR]
    delete_profile.restype = ctypes.c_long
    convert_sid = advapi32.ConvertSidToStringSidW
    convert_sid.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    convert_sid.restype = wintypes.BOOL

    profile_name = str(config["profile_name"])
    sid_pointer = ctypes.c_void_p()
    hr = create_profile(
        profile_name,
        "PaperForge local job",
        "Ephemeral networkless PaperForge execution sandbox",
        None,
        0,
        ctypes.byref(sid_pointer),
    )
    if ctypes.c_uint32(hr).value == 0x800700B7:
        hr = derive_sid(profile_name, ctypes.byref(sid_pointer))
    if hr != 0 or not sid_pointer.value:
        raise win_error(get_last_error())

    sid_text_pointer = wintypes.LPWSTR()
    if not convert_sid(sid_pointer, ctypes.byref(sid_text_pointer)):
        kernel32.LocalFree(sid_pointer)
        delete_profile(profile_name)
        raise win_error(get_last_error())
    sid_text = str(sid_text_pointer.value)
    kernel32.LocalFree(sid_text_pointer)

    grants: list[Path] = []
    attribute_buffer: Any = None
    attribute_list = ctypes.c_void_p()
    process_info = PROCESS_INFORMATION()
    job_handle = wintypes.HANDLE()
    duplicated_handles: list[int] = []
    process_finished = False
    try:
        for raw_path in config.get("read_roots", []):
            path = Path(str(raw_path)).resolve(strict=True)
            _grant(path, sid_text, "(OI)(CI)(RX)" if path.is_dir() else "(RX)")
            grants.append(path)
        for raw_path in config.get("write_roots", []):
            path = Path(str(raw_path)).resolve(strict=True)
            _grant(path, sid_text, "(OI)(CI)(M)" if path.is_dir() else "(M)")
            grants.append(path)

        size = ctypes.c_size_t()
        kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        attribute_buffer = ctypes.create_string_buffer(size.value)
        attribute_list = ctypes.cast(attribute_buffer, ctypes.c_void_p)
        if not kernel32.InitializeProcThreadAttributeList(
            attribute_list, 1, 0, ctypes.byref(size)
        ):
            raise win_error(get_last_error())
        capabilities = SECURITY_CAPABILITIES(
            AppContainerSid=sid_pointer,
            Capabilities=None,
            CapabilityCount=0,
            Reserved=0,
        )
        if not kernel32.UpdateProcThreadAttribute(
            attribute_list,
            0,
            0x00020009,
            ctypes.byref(capabilities),
            ctypes.sizeof(capabilities),
            None,
            None,
        ):
            raise win_error(get_last_error())

        current_process = kernel32.GetCurrentProcess()
        for standard_id in (-10, -11, -12):
            source = kernel32.GetStdHandle(ctypes.c_ulong(standard_id).value)
            duplicate = wintypes.HANDLE()
            if not kernel32.DuplicateHandle(
                current_process,
                source,
                current_process,
                ctypes.byref(duplicate),
                0,
                True,
                0x00000002,
            ):
                raise win_error(get_last_error())
            duplicated_handles.append(int(duplicate.value or 0))

        startup = STARTUPINFOEXW()
        startup.StartupInfo.cb = ctypes.sizeof(startup)
        startup.StartupInfo.dwFlags = 0x00000100
        startup.StartupInfo.hStdInput = duplicated_handles[0]
        startup.StartupInfo.hStdOutput = duplicated_handles[1]
        startup.StartupInfo.hStdError = duplicated_handles[2]
        startup.lpAttributeList = attribute_list

        command = [str(part) for part in config["command"]]
        command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(command))
        environment = {
            str(key): str(value)
            for key, value in dict(config.get("environment", {})).items()
        }
        environment_block = ctypes.create_unicode_buffer(
            "\0".join(
                f"{key}={value}"
                for key, value in sorted(environment.items(), key=lambda item: item[0].upper())
            )
            + "\0\0"
        )
        creation_flags = 0x00080000 | 0x00000400 | 0x00000004
        if not kernel32.CreateProcessW(
            command[0],
            command_line,
            None,
            None,
            True,
            creation_flags,
            environment_block,
            str(config["cwd"]),
            ctypes.byref(startup),
            ctypes.byref(process_info),
        ):
            raise win_error(get_last_error())

        job_handle = kernel32.CreateJobObjectW(None, None)
        if not job_handle:
            raise win_error(get_last_error())
        limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        limits.BasicLimitInformation.LimitFlags = 0x00002000
        if not kernel32.SetInformationJobObject(
            job_handle,
            9,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            raise win_error(get_last_error())
        if not kernel32.AssignProcessToJobObject(job_handle, process_info.hProcess):
            raise win_error(get_last_error())

        cancelled = False

        def terminate(_signum: int, _frame: Any) -> None:
            nonlocal cancelled
            cancelled = True
            if process_info.hProcess:
                kernel32.TerminateProcess(process_info.hProcess, 1)

        signal.signal(signal.SIGTERM, terminate)
        signal.signal(signal.SIGINT, terminate)
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, terminate)
        kernel32.ResumeThread(process_info.hThread)
        kernel32.WaitForSingleObject(process_info.hProcess, 0xFFFFFFFF)
        process_finished = True
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(
            process_info.hProcess, ctypes.byref(exit_code)
        ):
            raise win_error(get_last_error())
        return 1 if cancelled else int(exit_code.value)
    finally:
        if process_info.hProcess and not process_finished:
            kernel32.TerminateProcess(process_info.hProcess, 1)
        if process_info.hThread:
            kernel32.CloseHandle(process_info.hThread)
        if process_info.hProcess:
            kernel32.CloseHandle(process_info.hProcess)
        if job_handle:
            kernel32.CloseHandle(job_handle)
        for handle in duplicated_handles:
            kernel32.CloseHandle(handle)
        if attribute_list:
            kernel32.DeleteProcThreadAttributeList(attribute_list)
        for path in reversed(grants):
            _remove_grant(path, sid_text)
        kernel32.LocalFree(sid_pointer)
        delete_profile(profile_name)


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1:
        return 2
    config_path = Path(arguments[0]).expanduser().resolve(strict=True)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(config, dict) or config.get("schema") != "paperforge.windows-appcontainer/v1":
        return 2
    try:
        return _run_appcontainer(config)
    except BaseException as exc:
        print(f"Windows AppContainer failed: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
