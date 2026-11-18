from __future__ import annotations

import sys
import time
from collections.abc import Callable

_MAX_ATTEMPTS = 50
_MAX_DELAY = 0.05
_MAX_TOTAL_DELAY = 2.0


def retry_win_sharing[T](operation: Callable[[], T]) -> T:
    """Retry an operation interrupted by a transient Windows sharing violation."""
    attempts = 0
    started = time.monotonic()
    while True:
        try:
            return operation()
        except PermissionError:
            attempts += 1
            if sys.platform != "win32" or attempts >= _MAX_ATTEMPTS:
                raise
            remaining = _MAX_TOTAL_DELAY - (time.monotonic() - started)
            if remaining <= 0:
                raise
            delay = min(0.001 * (2 ** (attempts - 1)), _MAX_DELAY, remaining)
            time.sleep(delay)
