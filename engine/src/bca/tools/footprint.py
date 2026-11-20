from __future__ import annotations

import argparse
import csv
import os
import subprocess
import sys
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any


def _win_dll() -> Any:
    return getattr(__import__("ctypes"), "WinDLL")  # noqa: B009


def _win_error() -> int:
    return int(getattr(__import__("ctypes"), "get_last_error")())  # noqa: B009


def _windows_children(root: int) -> set[int]:
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):
        _fields_ = [
            ("size", wintypes.DWORD),
            ("usage", wintypes.DWORD),
            ("pid", wintypes.DWORD),
            ("heap", ctypes.c_void_p),
            ("module", wintypes.DWORD),
            ("threads", wintypes.DWORD),
            ("parent", wintypes.DWORD),
            ("priority", wintypes.LONG),
            ("flags", wintypes.DWORD),
            ("exe", wintypes.WCHAR * 260),
        ]

    kernel = _win_dll()("kernel32", use_last_error=True)
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise OSError(_win_error(), "CreateToolhelp32Snapshot failed")
    try:
        entry = Entry()
        entry.size = ctypes.sizeof(Entry)
        children: dict[int, set[int]] = {}
        if kernel.Process32FirstW(snapshot, ctypes.byref(entry)):
            while True:
                children.setdefault(int(entry.parent), set()).add(int(entry.pid))
                if not kernel.Process32NextW(snapshot, ctypes.byref(entry)):
                    break
        result = {root}
        pending = [root]
        while pending:
            for child in children.get(pending.pop(), ()):
                if child not in result:
                    result.add(child)
                    pending.append(child)
        return result
    finally:
        kernel.CloseHandle(snapshot)


def _windows_private(pid: int) -> int:
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("page_fault_count", wintypes.DWORD),
            ("peak_working_set", ctypes.c_size_t),
            ("working_set", ctypes.c_size_t),
            ("quota_peak_paged", ctypes.c_size_t),
            ("quota_paged", ctypes.c_size_t),
            ("quota_peak_nonpaged", ctypes.c_size_t),
            ("quota_nonpaged", ctypes.c_size_t),
            ("pagefile_usage", ctypes.c_size_t),
            ("peak_pagefile_usage", ctypes.c_size_t),
            ("private_usage", ctypes.c_size_t),
        ]

    kernel = _win_dll()("kernel32", use_last_error=True)
    handle = kernel.OpenProcess(0x0400 | 0x0010, False, pid)
    if not handle:
        raise OSError(_win_error(), f"OpenProcess failed for {pid}")
    try:
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if not kernel.K32GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            raise OSError(_win_error(), f"K32GetProcessMemoryInfo failed for {pid}")
        return int(counters.private_usage)
    finally:
        kernel.CloseHandle(handle)


def _mac_children(pid: int) -> set[int]:
    result = {pid}
    pending = [pid]
    while pending:
        current = pending.pop()
        completed = subprocess.run(["pgrep", "-P", str(current)], capture_output=True, text=True, check=False)
        for line in completed.stdout.splitlines():
            child = int(line)
            if child not in result:
                result.add(child)
                pending.append(child)
    return result


def _mac_private(pid: int) -> int:
    completed = subprocess.run(
        ["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True, check=True
    )
    line = completed.stdout.strip()
    if not line:
        raise ProcessLookupError(pid)
    return int(line) * 1024


def _linux_private(pid: int) -> int:
    rollup = Path(f"/proc/{pid}/smaps_rollup")
    try:
        values = [
            int(line.split()[1])
            for line in rollup.read_text().splitlines()
            if line.startswith(("Private_Clean:", "Private_Dirty:"))
        ]
        if values:
            return sum(values) * 1024
    except OSError:
        pass
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) * 1024
    raise ProcessLookupError(pid)


def process_tree(pid: int) -> set[int]:
    if sys.platform == "win32":
        return _windows_children(pid)
    if sys.platform == "darwin":
        return _mac_children(pid)
    result = {pid}
    changed = True
    while changed:
        changed = False
        for child in Path("/proc").glob("*/stat"):
            try:
                fields = child.read_text().split()
                child_pid, parent = int(fields[0]), int(fields[3])
            except (OSError, ValueError, IndexError):
                continue
            if parent in result and child_pid not in result:
                result.add(child_pid)
                changed = True
    return result


def private_working_set(pid: int) -> int:
    measure = (
        _windows_private
        if sys.platform == "win32"
        else _mac_private
        if sys.platform == "darwin"
        else _linux_private
    )
    total = 0
    for member in process_tree(pid):
        try:
            total += measure(member)
        except (OSError, ProcessLookupError):
            continue
    return total


def measure_csv(pid: int, interval: float, duration: float, output: Path) -> None:
    started = time.monotonic()
    with output.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(("elapsed_seconds", "pid", "private_working_set_bytes"))
        while True:
            elapsed = time.monotonic() - started
            writer.writerow((f"{elapsed:.3f}", pid, private_working_set(pid)))
            stream.flush()
            if elapsed >= duration:
                break
            time.sleep(min(interval, max(0.0, duration - elapsed)))


def simulate(leads: int) -> tuple[int, int]:
    from tempfile import TemporaryDirectory

    from tests.sim.harness import Population, Scenario, run_scenario

    with TemporaryDirectory(prefix="bca-footprint-", ignore_cleanup_errors=True) as directory:
        result = run_scenario(Scenario("footprint", Population(normal=leads)), Path(directory))
        try:
            return result.peak_memory, private_working_set(os.getpid())
        finally:
            result.store.close()


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure a process tree or synthetic simulator footprint")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--pid", type=int)
    group.add_argument("--sim", type=int)
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--duration", type=float, default=600.0)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.pid is not None:
        if args.out is None or args.interval <= 0 or args.duration < 0:
            parser.error("--pid requires --out, a positive --interval, and a non-negative --duration")
        measure_csv(args.pid, args.interval, args.duration, args.out)
    else:
        if args.sim <= 0:
            parser.error("--sim must be positive")
        peak, steady = simulate(args.sim)
        print(f"peak simulator tracemalloc: {peak} bytes")
        print(f"steady process-tree private working set: {steady} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
