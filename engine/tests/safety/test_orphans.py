from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast

import pytest

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
    pid_file = tmp_path / "pids"
    code = (
        "import pathlib, subprocess, sys, time; "
        "children=[subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']) for _ in range(2)]; "
        "pathlib.Path(sys.argv[1]).write_text(str(children[0].pid)+'\\n'+"
        "str(children[1].pid)); time.sleep(60)"
    )
    parent = subprocess.Popen([sys.executable, "-c", code, str(pid_file)], start_new_session=os.name != "nt")
    deadline = time.monotonic() + 10
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert pid_file.exists()
    descendants = [int(line) for line in pid_file.read_text().splitlines()]
    if os.name == "nt":
        job = create_kill_on_close_job()
        assert job is not None
        try:
            try:
                assign_process(job, parent.pid)
            except OSError as error:
                if getattr(error, "winerror", None) == 5:
                    pytest.skip("Windows process is already in a non-breakaway job")
                raise
        finally:
            close_job(job)
    else:
        cast(Any, os).killpg(parent.pid, cast(Any, signal).SIGKILL)
    parent.wait(timeout=5)
    deadline = time.monotonic() + 10
    while any(pid_alive(pid) for pid in descendants) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not any(pid_alive(pid) for pid in descendants)
