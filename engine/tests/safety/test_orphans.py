from __future__ import annotations

import asyncio
import ctypes
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from bca.claude.launch import _terminate_process_tree
from bca.claude.winjob import (
    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
    _kernel32,
    assign_process,
    close_job,
    create_kill_on_close_job,
)
from bca.procutil import pid_alive


@pytest.mark.skipif(os.name != "nt", reason="Windows job objects are Windows-only")
def test_winjob_sets_kill_on_close_flag() -> None:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    job = create_kill_on_close_job()
    assert job is not None
    try:
        kernel = _kernel32()
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        returned = ctypes.c_uint32()
        assert kernel.QueryInformationJobObject(
            job,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
            ctypes.byref(returned),
        )
        assert info.BasicLimitInformation.LimitFlags & JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    finally:
        close_job(job)
        child.terminate()
        child.wait(timeout=10)


def test_killed_parent_leaves_no_descendants(tmp_path: Path) -> None:
    go_file = tmp_path / "go"
    pid_file = tmp_path / "pids"
    # A real script file: a one-line ``python -c`` cannot hold the wait loop.
    script = tmp_path / "parent.py"
    script.write_text(
        "import pathlib, subprocess, sys, time\n"
        "go = pathlib.Path(sys.argv[1])\n"
        "pids = pathlib.Path(sys.argv[2])\n"
        "while not go.exists():\n"
        "    time.sleep(0.02)\n"
        "children = [\n"
        "    subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']) for _ in range(2)\n"
        "]\n"
        "pids.write_text(str(children[0].pid) + '\\n' + str(children[1].pid))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
        newline="\n",
    )
    parent = subprocess.Popen(
        [sys.executable, str(script), str(go_file), str(pid_file)], start_new_session=os.name != "nt"
    )
    job: object | None = None
    try:
        if os.name == "nt":
            job = create_kill_on_close_job()
            assert job is not None
            assign_process(job, parent.pid)
        go_file.write_text("go\n", encoding="utf-8", newline="\n")
        deadline = time.monotonic() + 10
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pid_file.exists()
        descendants = [int(line) for line in pid_file.read_text().splitlines()]
        if os.name == "nt":
            assert job is not None
            close_job(job)
            job = None
        else:
            asyncio.run(_terminate_process_tree(_as_async_process(parent), None))
        parent.wait(timeout=5)
        deadline = time.monotonic() + 10
        while any(pid_alive(pid) for pid in descendants) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not any(pid_alive(pid) for pid in descendants)
    except OSError as error:
        if getattr(error, "winerror", None) == 5:
            pytest.skip("Windows runner has a nested job without breakaway")
        raise
    finally:
        if job is not None:
            close_job(job)
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        # Never leak the test's own processes, whatever happened above.
        if pid_file.exists():
            for line in pid_file.read_text().splitlines():
                pid = int(line)
                if pid_alive(pid):
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], check=False)
                    else:
                        os.kill(pid, 9)


def _as_async_process(process: subprocess.Popen[bytes]) -> Any:
    """Adapt the Popen handle for the launcher's async tree terminator."""

    class ProcessAdapter:
        pid = process.pid
        returncode = process.returncode

        async def wait(self) -> int:
            return process.wait()

        def kill(self) -> None:
            process.kill()

    return ProcessAdapter()
