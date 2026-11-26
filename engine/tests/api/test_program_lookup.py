"""Programs are launched the way the setup check finds them. Synthetic data only.

On Windows, npm installs Codex as `codex.cmd`. The engine launched the bare name `codex`, which
Windows cannot start ("file not found"), while the setup check (shutil.which) said Codex was
installed: the live app showed "the Codex program didn't start" with every check green.
"""

from __future__ import annotations

import asyncio
import os
import stat
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from bca.api.production import _program
from tests.api.test_production import make_backend


def _put_on_path(monkeypatch: pytest.MonkeyPatch, folder: Path) -> None:
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{os.environ.get('PATH', '')}")


def test_a_bare_name_is_looked_up_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if sys.platform == "win32":
        program = bin_dir / "synthetic-tool.cmd"
        program.write_text("@echo off\n", encoding="utf-8")
    else:
        program = bin_dir / "synthetic-tool"
        program.write_text("#!/bin/sh\n", encoding="utf-8")
        program.chmod(program.stat().st_mode | stat.S_IXUSR)
    _put_on_path(monkeypatch, bin_dir)
    assert os.path.normcase(str(_program("synthetic-tool"))) == os.path.normcase(str(program))


def test_a_path_or_an_unknown_name_is_used_as_given(tmp_path: Path) -> None:
    given = tmp_path / "tools" / "codex.exe"
    assert _program(str(given)) == given
    assert _program("synthetic-tool-that-is-not-installed") == Path("synthetic-tool-that-is-not-installed")


@pytest.mark.skipif(sys.platform != "win32", reason="npm's .cmd shims exist on Windows only")
def test_codex_installed_as_a_cmd_shim_starts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.fakes import fake_codex

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # What npm installs: a codex.cmd that starts the real program (here, the fake app-server).
    (bin_dir / "codex.cmd").write_text(
        f'@"{sys.executable}" "{Path(fake_codex.__file__)}" %*\r\n', encoding="utf-8"
    )
    _put_on_path(monkeypatch, bin_dir)
    monkeypatch.delenv("BCA_CODEX_BIN", raising=False)
    monkeypatch.delenv("BCA_TEST_FAKES", raising=False)
    backend = make_backend(tmp_path / "data")
    assert backend._codex_binary().name.lower() == "codex.cmd"

    async def read() -> Any:
        return await backend.call("allowance/status", {})

    status = cast(dict[str, Any], asyncio.run(read()))
    # The app-server started and answered, so the allowance is read, not left unknown.
    assert status["codex"] is not None
