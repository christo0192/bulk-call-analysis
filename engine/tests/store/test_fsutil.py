import sys
import time

import pytest

from bca.store import fsutil


def test_retry_win_sharing_retries_permission_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(time, "sleep", lambda _delay: None)
    calls = 0

    def operation() -> str:
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise PermissionError("sharing violation")
        return "done"

    assert fsutil.retry_win_sharing(operation) == "done"
    assert calls == 3
