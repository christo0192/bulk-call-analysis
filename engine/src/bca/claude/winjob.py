"""Windows Job Object containment for Claude and every descendant."""

from __future__ import annotations

import ctypes
import os
import sys
from typing import Any, cast

if sys.platform == "win32":
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

    def _kernel32() -> Any:
        return ctypes.WinDLL("kernel32", use_last_error=True)
else:
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0
    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 0

    def _kernel32() -> Any:
        raise OSError("Windows job objects are unavailable on this platform")


def assign_kill_on_close(pid: int) -> object | None:
    if os.name != "nt":
        return None
    try:
        kernel = _kernel32()
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        kernel.CreateJobObjectW.restype = ctypes.c_void_p
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel.AssignProcessToJobObject.restype = ctypes.c_int
        kernel.SetInformationJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        kernel.SetInformationJobObject.restype = ctypes.c_int
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle.restype = ctypes.c_int
        handle = kernel.CreateJobObjectW(None, None)
        if not handle:
            return None
        limits = (ctypes.c_byte * 144)()
        ctypes.cast(limits, ctypes.POINTER(ctypes.c_uint32))[0] = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(
            handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, limits, len(limits)
        ):
            kernel.CloseHandle(handle)
            return None
        process = kernel.OpenProcess(0x0200 | 0x0400 | 0x0001, False, pid)
        if not process or not kernel.AssignProcessToJobObject(handle, process):
            if process:
                kernel.CloseHandle(process)
            kernel.CloseHandle(handle)
            return None
        kernel.CloseHandle(process)
        return cast(object, handle)
    except (AttributeError, OSError, ctypes.ArgumentError):
        return None


def terminate_job(job: object) -> bool:
    if os.name != "nt" or not job:
        return False
    try:
        return bool(_kernel32().TerminateJobObject(ctypes.c_void_p(cast(int, job)), 1))
    except (AttributeError, OSError, TypeError):
        return False


def close_job(job: object | None) -> None:
    if os.name == "nt" and job:
        _kernel32().CloseHandle(ctypes.c_void_p(cast(int, job)))
