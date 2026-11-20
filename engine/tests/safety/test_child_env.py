from __future__ import annotations

import ast
import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from bca.childenv import DENIED_EXACT, build_child_env
from bca.claude.launch import build_turn_env
from bca.claude.profile import ClaudeProfile

_FORBIDDEN_DIRECT = {
    "run",
    "Popen",
    "call",
    "check_call",
    "check_output",
    "getoutput",
    "getstatusoutput",
    "create_subprocess_exec",
    "create_subprocess_shell",
    "system",
    "popen",
    "startfile",
}


class _SubprocessCallVisitor(ast.NodeVisitor):
    def __init__(self, module: str) -> None:
        self.module = module
        self.stack: list[str] = []
        self.aliases: dict[str, str] = {}
        self.calls: list[tuple[str, str, str]] = []

    def visit_Import(self, node: ast.Import) -> None:
        for item in node.names:
            self.aliases[item.asname or item.name.split(".")[0]] = item.name

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        prefix = node.module or ""
        for item in node.names:
            if item.name != "*":
                self.aliases[item.asname or item.name] = f"{prefix}.{item.name}".strip(".")

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_Call(self, node: ast.Call) -> None:
        qualified = self._qualified(node.func)
        if qualified is not None and self._is_forbidden(qualified):
            self.calls.append((self.module, self.stack[-1] if self.stack else "<module>", qualified))
        self.generic_visit(node)

    def _qualified(self, node: ast.expr) -> str | None:
        if isinstance(node, ast.Name):
            return self.aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            parent = self._qualified(node.value)
            return f"{parent}.{node.attr}" if parent else node.attr
        return None

    @staticmethod
    def _is_forbidden(qualified: str) -> bool:
        parts = qualified.split(".")
        if qualified.startswith(("subprocess.", "asyncio.")):
            return parts[-1] in _FORBIDDEN_DIRECT
        if qualified.startswith("os."):
            return parts[-1] in _FORBIDDEN_DIRECT or parts[-1].startswith(("spawn", "exec", "posix_spawn"))
        return qualified in {
            "multiprocessing.Process",
            "multiprocessing.Pool",
            "concurrent.futures.ProcessPoolExecutor",
        }


def _find_forbidden(source: str) -> list[tuple[str, str, str]]:
    visitor = _SubprocessCallVisitor("synthetic")
    visitor.visit(ast.parse(source))
    return visitor.calls


@pytest.mark.parametrize("launcher", ["codex", "claude"])
def test_launched_children_receive_no_denied_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launcher: str
) -> None:
    script = tmp_path / "env_probe.py"
    report = tmp_path / "environment.json"
    # The probe records its environment at a fixed path and exits; the launch itself is expected to fail
    # afterwards (the probe speaks no protocol). Only the recorded environment matters.
    script.write_text(
        f"import json, os\njson.dump(sorted(os.environ), open({str(report)!r}, 'w', encoding='utf-8'))\n",
        encoding="utf-8",
        newline="\n",
    )
    for name in DENIED_EXACT:
        monkeypatch.setenv(name, "synthetic-denied")
    if os.name == "nt":
        wrapper = tmp_path / "probe.cmd"
        wrapper.write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8", newline="\n")
        prefix = [str(wrapper)]
    else:
        prefix = [sys.executable, str(script)]

    async def exercise() -> None:
        if launcher == "codex":
            from bca.luna.appserver import CodexLaunch, start

            try:
                codex = await start(CodexLaunch(tuple(prefix), tmp_path / "codex-home", tmp_path))
            except Exception:
                return
            await asyncio.wait_for(codex.stop(), timeout=3)
            return

        from bca.claude.launch import SessionRef, run_turn

        profile = ClaudeProfile(tmp_path / "claude", tmp_path / "work")
        import bca.claude.auth as auth
        import bca.claude.launch as claude_launch

        monkeypatch.setattr(auth, "auth_status", lambda binary, current: type("Status", (), {"ok": True})())
        original = claude_launch.build_turn_command
        monkeypatch.setattr(
            claude_launch,
            "build_turn_command",
            lambda *args, **kwargs: [*prefix, *original(*args, **kwargs)[1:]],
        )
        try:
            async for _event in run_turn(
                Path(sys.executable),
                profile,
                tmp_path / "mcp",
                tmp_path / "brief",
                tmp_path / "settings",
                "brief",
                "operator",
                "claude-opus-5-5",
                session=SessionRef(uuid4()),
            ):
                pass
        except Exception:
            return

    asyncio.run(asyncio.wait_for(exercise(), timeout=10))
    received = set(json.loads(report.read_text(encoding="utf-8")))
    assert received.isdisjoint(DENIED_EXACT)


@pytest.mark.parametrize("launcher", ["codex", "claude"])
def test_no_api_key_reaches_any_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launcher: str) -> None:
    names = set(DENIED_EXACT) | {"SYNTHETIC_TOKEN", "SYNTHETIC_SECRET", "API_KEY"}
    for name in names:
        monkeypatch.setenv(name, "synthetic-secret")
    report = tmp_path / f"{launcher}.json"
    base = {name: "synthetic-secret" for name in names}
    # Denied names in the inherited environment are dropped; passing one explicitly raises ChildEnvError.
    child = build_child_env(base=base)
    assert not any(name in child for name in names)
    if launcher == "claude":
        env = build_turn_env(ClaudeProfile(tmp_path / "claude", tmp_path / "work"))
        assert not any(name in env for name in names)
    else:
        assert not any(name in build_child_env({"CODEX_HOME": str(tmp_path)}) for name in names)
    report.write_text(json.dumps(sorted(child)), encoding="utf-8", newline="\n")
    assert report.is_file()


def test_no_unlisted_subprocess_call_sites() -> None:
    root = Path(__file__).parents[2] / "src"
    allowed = {
        ("bca.api.production", "_start_codex_login"),
        ("bca.api.production", "shutdown"),
        ("bca.claude.auth", "auth_status"),
        ("bca.claude.auth", "start_login"),
        ("bca.claude.binary", "_verify_signature"),
        ("bca.claude.launch", "_terminate_process_tree"),
        ("bca.claude.launch", "run_turn"),
        ("bca.luna.appserver", "AppServer.stop"),
        ("bca.luna.appserver", "stop"),
        ("bca.luna.appserver", "start"),
        ("bca.mask.masker", "run_masking"),
        ("bca.mcp.server_common", "run_shim"),
        ("bca.setup.codexbin", "_verify_signature"),
        ("bca.signing", "verify_authenticode"),
        ("bca.tools.footprint", "_mac_children"),
        ("bca.tools.footprint", "_mac_private"),
    }
    found: set[tuple[str, str]] = set()
    for path in root.rglob("*.py"):
        module = ".".join(path.relative_to(root).with_suffix("").parts)
        visitor = _SubprocessCallVisitor(module)
        visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
        found.update((item[0], item[1]) for item in visitor.calls)
    assert found <= allowed, found - allowed


def test_subprocess_call_visitor_flags_all_forbidden_forms() -> None:
    source = """
import asyncio, multiprocessing, os, subprocess as sp
from asyncio import create_subprocess_exec as cse, create_subprocess_shell
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import Pool, Process
from os import execv, popen, spawnv, startfile, system
from subprocess import Popen, call, check_call, check_output, getoutput, getstatusoutput, run
def f():
    sp.run([]); sp.Popen([]); sp.call([]); sp.check_call([]); sp.check_output([])
    sp.getoutput('x'); sp.getstatusoutput('x'); Popen([]); call([]); check_call([])
    check_output([]); getoutput('x'); getstatusoutput('x'); run([])
    asyncio.create_subprocess_exec('x'); asyncio.create_subprocess_shell('x')
    cse('x'); create_subprocess_shell('x')
    os.system('x'); os.popen('x'); os.spawnv(0, 'x', []); os.execv('x', []); os.startfile('x')
    system('x'); popen('x'); spawnv(0, 'x', []); execv('x', []); startfile('x')
    multiprocessing.Process(); multiprocessing.Pool(); Process(); Pool(); ProcessPoolExecutor()
"""
    calls = _find_forbidden(source)
    assert len(calls) == 33
    assert {qualified for _, _, qualified in calls} >= {
        "subprocess.run",
        "asyncio.create_subprocess_exec",
        "os.spawnv",
        "multiprocessing.Process",
        "concurrent.futures.ProcessPoolExecutor",
    }
