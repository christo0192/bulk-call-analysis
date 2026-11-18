from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from bca.claude.launch import SessionRef, build_turn_command, build_turn_env
from bca.claude.profile import ClaudeProfile


def test_profile_settings_and_empty_project(tmp_path: Path) -> None:
    profile = ClaudeProfile(tmp_path / "config", tmp_path / "work", 17)
    settings = json.loads(profile.write_settings().read_text(encoding="utf-8"))
    assert settings["disableClaudeAiConnectors"] is True
    assert settings["syncClaudeAiSkills"] is False
    assert settings["syncClaudeAiPlugins"] is False
    assert settings["hooks"] == {}
    assert settings["cleanupPeriodDays"] == 17
    project = profile.project_workdir("project")
    assert list(project.iterdir()) == []


def test_command_is_subscription_only(tmp_path: Path) -> None:
    command = build_turn_command(
        Path("claude"),
        tmp_path,
        tmp_path / "mcp.json",
        tmp_path / "brief",
        tmp_path / "settings",
        "claude-opus-5-5",
        "high",
        4,
        SessionRef(uuid4()),
    )
    assert "--tools" in command and "" in command
    assert all(bad not in command for bad in ("--bare", "--fallback-model", "sonnet"))
    assert "--session-id" in command


def test_child_env_strips_api_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from bca import childenv

    monkeypatch.setattr(childenv.os, "environ", {"ANTHROPIC_API_KEY": "secret", "PATH": "p"})  # type: ignore[attr-defined]
    env = build_turn_env(ClaudeProfile(tmp_path / "config", tmp_path / "work"))
    assert "ANTHROPIC_API_KEY" not in env
    assert Path(env["CLAUDE_CONFIG_DIR"]).is_absolute()
