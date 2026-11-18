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
    assert set(settings) == {
        "disableClaudeAiConnectors",
        "syncClaudeAiSkills",
        "syncClaudeAiPlugins",
        "hooks",
        "modelPicker",
        "fastMode",
        "cleanupPeriodDays",
    }
    assert settings["disableClaudeAiConnectors"] is True
    assert settings["syncClaudeAiSkills"] is False
    assert settings["syncClaudeAiPlugins"] is False
    assert settings["hooks"] == {}
    assert settings["cleanupPeriodDays"] == 17
    if "ultracode" in settings:
        assert settings["ultracode"] is False
    project = profile.project_workdir("project")
    assert list(project.iterdir()) == []


def test_command_has_contract_flags_and_session_modes(tmp_path: Path) -> None:
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
    for flag in (
        "-p",
        "--verbose",
        "--input-format",
        "--output-format",
        "--include-partial-messages",
        "--session-id",
        "--model",
        "--effort",
        "--tools",
        "--permission-mode",
        "--allowedTools",
        "--mcp-config",
        "--strict-mcp-config",
        "--setting-sources",
        "--disable-slash-commands",
        "--append-system-prompt-file",
        "--settings",
        "--max-turns",
    ):
        assert flag in command
    assert "--tools" in command and "" in command
    assert all(bad not in command for bad in ("--bare", "--fallback-model", "--console"))
    assert "--session-id" in command
    resumed = build_turn_command(
        Path("claude"),
        tmp_path,
        tmp_path / "mcp",
        tmp_path / "brief",
        tmp_path / "settings",
        "claude-opus-5-5",
        session=SessionRef(uuid4(), resumed=True),
    )
    assert "--resume" in resumed and "--session-id" not in resumed


@pytest.mark.parametrize("alias", ["opus", "sonnet", "haiku", "fable", "default"])
def test_model_aliases_are_rejected(tmp_path: Path, alias: str) -> None:
    with pytest.raises(ValueError):
        build_turn_command(
            Path("claude"), tmp_path, tmp_path / "mcp", tmp_path / "brief", tmp_path / "settings", alias
        )


def test_effort_defaults_to_high(tmp_path: Path) -> None:
    command = build_turn_command(
        Path("claude"),
        tmp_path,
        tmp_path / "mcp",
        tmp_path / "brief",
        tmp_path / "settings",
        "claude-opus-5-5",
    )
    assert command[command.index("--effort") + 1] == "high"


def test_child_env_strips_api_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_CUSTOM_HEADERS",
    ):
        monkeypatch.setenv(name, "planted")
    env = build_turn_env(ClaudeProfile(tmp_path / "config", tmp_path / "work"))
    assert not any(
        name in env
        for name in (
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_CUSTOM_HEADERS",
        )
    )
    assert Path(env["CLAUDE_CONFIG_DIR"]).is_absolute()
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert env["DISABLE_AUTOUPDATER"] == "1"
    assert env["MCP_TIMEOUT"] == "120000"
    assert env["ENABLE_TOOL_SEARCH"] == "0"
