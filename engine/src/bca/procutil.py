from __future__ import annotations

import sys

if sys.platform == "win32":
    import ctypes

    _SYNCHRONIZE = 0x00100000
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _WAIT_TIMEOUT = 0x00000102
    _ERROR_INVALID_PARAMETER = 87

    def pid_alive(pid: int) -> bool:
        """Return whether a process with *pid* currently exists."""
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int

        handle = kernel32.OpenProcess(_SYNCHRONIZE | _PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
        if not handle:
            error = ctypes.get_last_error()
            if error == _ERROR_INVALID_PARAMETER:
                return False
            raise ctypes.WinError(error)
        try:
            return bool(kernel32.WaitForSingleObject(handle, 0) == _WAIT_TIMEOUT)
        finally:
            kernel32.CloseHandle(handle)
else:
    import os

    def pid_alive(pid: int) -> bool:
        """Return whether a process with *pid* currently exists."""
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        else:
            return True
