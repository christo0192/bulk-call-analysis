from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest

import bca.claude.launch as launch
from bca.childenv import build_child_env
from bca.claude.launch import SessionRef, TurnGuardError, run_turn, stop_turn
from bca.claude.profile import ClaudeProfile

FAKE = Path(__file__).parents[1] / "fakes" / "fake_claude.py"


def fake_environment(profile: ClaudeProfile, scenario: str, **extra: str) -> dict[str, str]:
    return build_child_env(
        {
            "CLAUDE_CONFIG_DIR": str(profile.config_dir.resolve()),
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
            "DISABLE_AUTOUPDATER": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "ENABLE_TOOL_SEARCH": "0",
            "MCP_TIMEOUT": "120000",
            "FAKE_CLAUDE_SCENARIO": scenario,
            **extra,
        }
    )


def test_run_turn_yields_fake_events_and_sends_both_messages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = ClaudeProfile(tmp_path / "config", tmp_path / "work")
    stdin_log = tmp_path / "stdin.jsonl"
    original_builder = launch.build_turn_command
    monkeypatch.setattr(
        launch,
        "build_turn_env",
        lambda current: fake_environment(current, "normal", FAKE_CLAUDE_STDIN_LOG=str(stdin_log)),
    )
    monkeypatch.setattr(
        launch,
        "build_turn_command",
        lambda *args, **kwargs: [sys.executable, str(FAKE), *original_builder(*args, **kwargs)[1:]],
    )
    session = SessionRef(uuid4())

    async def collect() -> list[object]:
        return [
            event
            async for event in run_turn(
                Path(sys.executable),
                profile,
                tmp_path / "mcp",
                tmp_path / "brief",
                tmp_path / "settings",
                "brief text",
                "operator text",
                "claude-opus-5-5",
                session=session,
            )
        ]

    events = asyncio.run(collect())
    assert [event.kind for event in events] == ["system", "result"]  # type: ignore[attr-defined]
    payload = json.loads(stdin_log.read_text(encoding="utf-8"))
    assert payload["message"]["content"] == "brief text\n\noperator text"


def test_bad_init_aborts_and_kills_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profile = ClaudeProfile(tmp_path / "config", tmp_path / "work")
    original_builder = launch.build_turn_command
    monkeypatch.setattr(
        launch,
        "build_turn_env",
        lambda current: fake_environment(current, "bad_init"),
    )
    monkeypatch.setattr(
        launch,
        "build_turn_command",
        lambda *args, **kwargs: [sys.executable, str(FAKE), *original_builder(*args, **kwargs)[1:]],
    )

    async def collect() -> None:
        async for _event in run_turn(
            Path(sys.executable),
            profile,
            tmp_path / "mcp",
            tmp_path / "brief",
            tmp_path / "settings",
            "brief",
            "operator",
            "claude-opus-5-5",
        ):
            pass

    with pytest.raises(TurnGuardError):
        asyncio.run(collect())


def test_stop_turn_kills_process_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profile = ClaudeProfile(tmp_path / "config", tmp_path / "work")
    child_pid_file = tmp_path / "child.pid"
    original_builder = launch.build_turn_command
    monkeypatch.setattr(
        launch,
        "build_turn_env",
        lambda current: fake_environment(current, "spawn_child", FAKE_CLAUDE_CHILD_PID=str(child_pid_file)),
    )
    monkeypatch.setattr(
        launch,
        "build_turn_command",
        lambda *args, **kwargs: [sys.executable, str(FAKE), *original_builder(*args, **kwargs)[1:]],
    )
    session = SessionRef(uuid4())
    started = asyncio.Event()

    async def consume() -> None:
        async for _event in run_turn(
            Path(sys.executable),
            profile,
            tmp_path / "mcp",
            tmp_path / "brief",
            tmp_path / "settings",
            "brief",
            "operator",
            "claude-opus-5-5",
            session=session,
        ):
            pass

    async def stop_after_start() -> None:
        task = asyncio.create_task(consume())
        for _ in range(100):
            if child_pid_file.exists() and session.session_id in launch._processes:
                started.set()
                break
            await asyncio.sleep(0.01)
        await started.wait()
        child_pid = int(child_pid_file.read_text(encoding="utf-8"))
        stop_turn(session)
        await task
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)

    asyncio.run(stop_after_start())
