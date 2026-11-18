"""Best-effort Windows Job Object support for process-tree cleanup."""

from __future__ import annotations

import os
import subprocess
from typing import Any, cast


def assign_kill_on_close(process: subprocess.Popen[bytes] | subprocess.Popen[str]) -> object | None:
    if os.name != "nt":
        return None
    # Kept behind the platform gate so importing the bridge is safe on POSIX.
    try:
        import ctypes

        kernel = ctypes.windll.kernel32
        handle = kernel.CreateJobObjectW(None, None)
        if not handle:
            return None
        process_handle = cast(Any, process)._handle
        if not kernel.AssignProcessToJobObject(handle, int(process_handle)):
            return None
        return cast(object, handle)
    except (AttributeError, OSError):
        return None
