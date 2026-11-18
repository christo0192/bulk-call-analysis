from __future__ import annotations

import subprocess
import sys

from bca.procutil import pid_alive


def test_pid_alive_for_live_and_exited_child() -> None:
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert pid_alive(live.pid)
    finally:
        live.terminate()
        live.wait()

    assert not pid_alive(live.pid)
