"""Which program file the engine starts for Claude and Codex. Synthetic data only.

The engine launched the bare name `codex`, which Windows cannot start when npm installed it (the
file is `codex.cmd`), while the setup row said Codex was found. Starting a `.cmd` instead would run
it through cmd.exe, where text on the command line (a Settings model name, a data folder named like
"R&D") is read as commands. So the engine starts a real program file only, never a .cmd or .bat.
"""

from __future__ import annotations

import asyncio
import os
import stat
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from bca.api import production
from bca.api.production import ProductionBackend, _program
from bca.api.server import RpcError
from bca.claude.launch import build_turn_command
from tests.api.test_production import make_backend

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows program lookup")
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="PATH lookup outside Windows")


def _only_on_path(monkeypatch: pytest.MonkeyPatch, *folders: Path) -> None:
    # Only these folders, so a real installed program can never be found by a test.
    monkeypatch.setenv("PATH", os.pathsep.join(str(folder) for folder in folders))


def _file(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _npm_codex(shim_dir: Path) -> Path:
    """npm's layout: codex.cmd in the shim folder, the native codex.exe inside the package."""
    _file(shim_dir / "codex.cmd", "@echo off\r\n")
    _file(shim_dir / "codex", "#!/bin/sh\n")
    return _file(
        shim_dir
        / "node_modules/@openai/codex/node_modules/@openai/codex-win32-x64"
        / "vendor/x86_64-pc-windows-msvc/bin/codex.exe"
    )


@windows_only
def test_a_bare_name_finds_only_an_exe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    npm, native = tmp_path / "npm", tmp_path / "local-bin"
    _file(npm / "claude.cmd", "@echo off\r\n")
    exe = _file(native / "claude.exe")
    # An old npm claude.cmd earlier on PATH must not win over the native claude.exe.
    _only_on_path(monkeypatch, npm, native)
    assert _program("claude") == exe.absolute()


@windows_only
def test_a_lone_cmd_shim_is_never_started(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    npm = tmp_path / "npm"
    _file(npm / "claude.cmd", "@echo off\r\n")
    _file(npm / "tool.bat", "@echo off\r\n")
    _only_on_path(monkeypatch, npm)
    assert _program("claude") == Path("claude")
    assert _program("tool") == Path("tool")
    assert not _program("claude").is_file()


@windows_only
def test_npm_codex_resolves_to_its_native_program(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    npm = tmp_path / "npm"
    native = _npm_codex(npm)
    _only_on_path(monkeypatch, npm)
    assert _program("codex") == native.resolve()
    # A configured path to the shim (with or without .cmd) gets the same native program.
    assert _program(str(npm / "codex")) == native.resolve()
    assert _program(str(npm / "codex.cmd")) == native.resolve()


@windows_only
def test_a_configured_path_without_exe_finds_the_exe(tmp_path: Path) -> None:
    exe = _file(tmp_path / "tools" / "codex.exe")
    assert _program(str(tmp_path / "tools" / "codex")) == exe.absolute()
    assert _program(str(exe)) == exe.absolute()


@posix_only
def test_a_bare_name_is_looked_up_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    program = _file(tmp_path / "bin" / "synthetic-tool", "#!/bin/sh\n")
    program.chmod(program.stat().st_mode | stat.S_IXUSR)
    _only_on_path(monkeypatch, tmp_path / "bin")
    assert _program("synthetic-tool") == program.absolute()


def test_an_unknown_name_is_used_as_given(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _only_on_path(monkeypatch, tmp_path)
    assert _program("synthetic-tool-not-installed") == Path("synthetic-tool-not-installed")


@windows_only
@pytest.mark.parametrize("layout", ["npm", "missing", "lone-cmd"])
def test_the_codex_row_and_the_launch_use_the_same_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layout: str
) -> None:
    npm = tmp_path / "npm"
    npm.mkdir(parents=True)
    native = _npm_codex(npm) if layout == "npm" else None
    if layout == "lone-cmd":
        _file(npm / "codex.cmd", "@echo off\r\n")
    _only_on_path(monkeypatch, npm)
    monkeypatch.delenv("BCA_CODEX_BIN", raising=False)
    monkeypatch.delenv("BCA_TEST_FAKES", raising=False)
    launched: list[object] = []

    async def record(launch: Any) -> Any:
        launched.append(launch.exe)
        raise OSError("synthetic: not started in this test")

    monkeypatch.setattr(production, "start_app_server", record)
    backend = make_backend(tmp_path / "data")

    async def calls() -> Any:
        checks = await backend.call("setup/runChecks", {})
        await backend.call("allowance/status", {})
        return checks

    checks = asyncio.run(calls())
    row = next(item for item in cast(dict[str, Any], checks)["items"] if item["name"] == "codex_copy")
    assert launched, "the launch was attempted"
    started = Path(str(launched[-1]))
    assert started.suffix.lower() not in {".cmd", ".bat"}
    if native is not None:
        assert row["status"] == "ok"
        assert started == native.resolve()
    else:
        # Nothing startable: the row says so, and the launch gets the bare name (it fails).
        assert row["status"] == "failed"
        assert started == Path("codex")


@pytest.mark.parametrize(
    "model",
    [
        "claude-opus-5-5&calc&",
        "claude-opus-%OS%",
        "claude-opus^5",
        "claude-opus 5",
        'claude-"x"',
        "claude-Opus",
    ],
)
def test_claude_model_names_are_letters_digits_dots_and_dashes(tmp_path: Path, model: str) -> None:
    with pytest.raises(RpcError) as error:
        ProductionBackend._validate_settings({"claudeModel": model, "confirmModelChange": True})
    assert cast(dict[str, Any], error.value.data) == {"field": "claudeModel", "reason": "invalid model id"}
    with pytest.raises(ValueError):
        build_turn_command(
            Path("claude"),
            tmp_path / "profile",
            tmp_path / "mcp.json",
            tmp_path / "playbook.md",
            tmp_path / "settings.json",
            model,
        )


def test_a_pinned_claude_model_name_is_still_accepted(tmp_path: Path) -> None:
    ProductionBackend._validate_settings({"claudeModel": "claude-opus-5-5", "confirmModelChange": True})
    command = build_turn_command(
        Path("claude"),
        tmp_path / "profile",
        tmp_path / "mcp.json",
        tmp_path / "playbook.md",
        tmp_path / "settings.json",
        "claude-opus-5-5",
    )
    assert "claude-opus-5-5" in command
