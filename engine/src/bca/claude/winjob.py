"""Windows Job Object containment for Claude and every descendant."""

from __future__ import annotations

import ctypes
import logging
import os
import sys
from typing import Any, ClassVar, cast

LOGGER = logging.getLogger(__name__)


def _last_error() -> int:
    return int(getattr(ctypes, "get_last_error")())  # noqa: B009


def _windows_error(error: int) -> OSError:
    return cast(OSError, getattr(ctypes, "WinError")(error))  # noqa: B009


if sys.platform == "win32":
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_void_p),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", ctypes.c_byte * 48),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    def _kernel32() -> Any:
        return ctypes.WinDLL("kernel32", use_last_error=True)
else:
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0
    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 0

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_: ClassVar[list[tuple[str, Any]]] = []

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_: ClassVar[list[tuple[str, Any]]] = []

    def _kernel32() -> Any:
        raise OSError("Windows job objects are unavailable on this platform")


def create_kill_on_close_job() -> object | None:
    if os.name != "nt":
        return None
    try:
        kernel = _kernel32()
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        kernel.CreateJobObjectW.restype = ctypes.c_void_p
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
        limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(
            handle,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            kernel.CloseHandle(handle)
            return None
        queried = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        returned = ctypes.c_uint32()
        if not kernel.QueryInformationJobObject(
            handle,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(queried),
            ctypes.sizeof(queried),
            ctypes.byref(returned),
        ):
            kernel.CloseHandle(handle)
            raise _windows_error(_last_error())
        if not queried.BasicLimitInformation.LimitFlags & JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE:
            kernel.CloseHandle(handle)
            raise RuntimeError("job object kill-on-close flag was not retained")
        return cast(object, handle)
    except (AttributeError, OSError, ctypes.ArgumentError, RuntimeError) as error:
        LOGGER.warning("failed to create containment job: %s", error)
        return None


def assign_process(job: object, pid: int) -> None:
    if os.name != "nt":
        return
    kernel = _kernel32()
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel.AssignProcessToJobObject.restype = ctypes.c_int
    process = kernel.OpenProcess(0x0200 | 0x0400 | 0x0001, False, pid)
    if not process:
        raise _windows_error(_last_error())
    try:
        if not kernel.AssignProcessToJobObject(job, process):
            raise _windows_error(_last_error())
    finally:
        kernel.CloseHandle(process)


def assign_kill_on_close(pid: int) -> object | None:
    if os.name != "nt":
        return None
    handle = create_kill_on_close_job()
    if handle is None:
        return None
    try:
        assign_process(handle, pid)
    except (AttributeError, OSError, ctypes.ArgumentError) as error:
        if getattr(error, "winerror", None) == 5:
            LOGGER.warning("cannot assign process %s to job: process is already in a non-breakaway job", pid)
        else:
            LOGGER.warning("failed to assign process %s to containment job: %s", pid, error)
        close_job(handle)
        return None
    return handle


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
