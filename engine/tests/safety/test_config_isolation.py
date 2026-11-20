from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path

import pytest

from bca.setup.appdirs import directories
from tests.api.test_production_chat import make_backend
from tests.sim.harness import Scenario, run_scenario


def _snapshot(root: Path) -> dict[str, tuple[str, int, int]]:
    result: dict[str, tuple[str, int, int]] = {}
    for path in root.rglob("*"):
        if path.is_file():
            stat = path.stat()
            result[str(path.relative_to(root))] = (
                hashlib.sha256(path.read_bytes()).hexdigest(),
                stat.st_mtime_ns,
                stat.st_size,
            )
    return result


def test_operator_claude_and_codex_dirs_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "operator-home"
    claude = home / ".claude"
    codex = home / ".codex"
    claude.mkdir(parents=True)
    codex.mkdir()
    (claude / "sentinel").write_text("claude-sentinel\n", encoding="utf-8", newline="\n")
    (codex / "sentinel").write_text("codex-sentinel\n", encoding="utf-8", newline="\n")
    before = {str(path): _snapshot(path) for path in (claude, codex)}
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("BCA_DATA_DIR", str(tmp_path / "app-data"))
    (tmp_path / ".bca-test-fakes").touch()
    monkeypatch.setenv("BCA_TEST_FAKES", "1")

    sim_root = tmp_path / "sim"
    sim_root.mkdir()
    result = run_scenario(Scenario("small-happy"), sim_root)
    backend = make_backend(tmp_path / "chat")
    from tests.api.test_production_chat import allow_fakes

    allow_fakes(monkeypatch, tmp_path / "chat")
    chat_result = asyncio.run(backend.call("chat/send", {"project": "project-one", "text": "synthetic"}))
    assert isinstance(chat_result, dict)
    assert chat_result["status"] == "completed"
    assert result.state.value == "done"
    assert _snapshot(claude) == before[str(claude)]
    assert _snapshot(codex) == before[str(codex)]
    # Removing CLAUDE_CONFIG_DIR from build_turn_env makes fake Claude write this
    # marker under the fake HOME, so the isolation assertion below fails.
    assert (tmp_path / "chat" / "claude" / "fake-claude-start.marker").is_file()
    assert (sim_root / "codex-home" / "fake-codex-start.marker").is_file()
    assert not (claude / "fake-claude-start.marker").exists()
    assert not (codex / "fake-codex-start.marker").exists()
    app_dirs = directories(home=home, env=os.environ, system="Windows" if os.name == "nt" else "Linux")
    assert app_dirs.app_data != claude and app_dirs.app_data != codex
    assert (tmp_path / "chat" / "claude").is_dir()
