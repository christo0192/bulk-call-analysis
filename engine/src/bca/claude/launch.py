from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import subprocess
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

from bca.childenv import build_child_env

from .profile import ENV_DISABLE_MEMORY, ClaudeProfile
from .stream import Event, RateLimitGuard, TurnError, check_init, parse_line

FLAG_PROMPT = "-p"
FLAG_VERBOSE = "--verbose"
FLAG_INPUT = "--input-format"
FLAG_OUTPUT = "--output-format"
FLAG_PARTIAL = "--include-partial-messages"
FLAG_EFFORT = "--effort"
FLAG_TOOLS = "--tools"
FLAG_ALLOWED = "--allowedTools"
FLAG_PERMISSION = "--permission-mode"
FLAG_MCP = "--mcp-config"
FLAG_STRICT_MCP = "--strict-mcp-config"
FLAG_SETTINGS_SOURCES = "--setting-sources"
FLAG_NO_SLASH = "--disable-slash-commands"
FLAG_PLAYBOOK = "--append-system-prompt-file"
FLAG_SETTINGS = "--settings"
FLAG_MAX_TURNS = "--max-turns"
# Names confirmed by Claude Code CLI reference, 2026-11-18.
ENV_CONFIG = "CLAUDE_CONFIG_DIR"
ENV_DISABLE_UPDATE = "DISABLE_AUTOUPDATER"
ENV_NONESSENTIAL = "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"
ENV_TOOL_SEARCH = "ENABLE_TOOL_SEARCH"
ENV_MCP_TIMEOUT = "MCP_TIMEOUT"


@dataclass(frozen=True)
class SessionRef:
    session_id: UUID
    resumed: bool = False


@dataclass(frozen=True)
class TurnResult:
    events: tuple[Event, ...]
    error: TurnError | None = None
    exit_code: int | None = None


class TurnGuardError(RuntimeError):
    """Raised before model output is exposed when Claude violates the contract."""


def build_turn_command(
    binary: Path,
    profile: Path,
    mcp_config_path: Path,
    playbook_path: Path,
    settings_path: Path,
    model: str,
    effort: str = "high",
    max_turns: int = 25,
    session: SessionRef | None = None,
) -> list[str]:
    if model in {"opus", "sonnet", "haiku", "fable", "default"}:
        raise ValueError("Claude model must be a fully qualified subscription model name")
    if session is None:
        session = SessionRef(uuid4())
    session_flag = "--resume" if session.resumed else "--session-id"
    return [
        str(binary),
        FLAG_PROMPT,
        FLAG_VERBOSE,
        FLAG_INPUT,
        "stream-json",
        FLAG_OUTPUT,
        "stream-json",
        FLAG_PARTIAL,
        session_flag,
        str(session.session_id),
        "--model",
        model,
        FLAG_EFFORT,
        effort,
        FLAG_TOOLS,
        "",
        FLAG_PERMISSION,
        "dontAsk",
        FLAG_ALLOWED,
        "mcp__bq__*",
        "mcp__app__*",
        FLAG_MCP,
        str(mcp_config_path),
        FLAG_STRICT_MCP,
        FLAG_SETTINGS_SOURCES,
        "user",
        FLAG_NO_SLASH,
        FLAG_PLAYBOOK,
        str(playbook_path),
        FLAG_SETTINGS,
        str(settings_path),
        FLAG_MAX_TURNS,
        str(max_turns),
    ]


def build_turn_env(profile: ClaudeProfile) -> dict[str, str]:
    config_dir = profile.config_dir.resolve()
    return build_child_env(
        {
            ENV_CONFIG: str(config_dir),
            ENV_DISABLE_MEMORY: "1",
            ENV_DISABLE_UPDATE: "1",
            ENV_NONESSENTIAL: "1",
            ENV_TOOL_SEARCH: "0",
            ENV_MCP_TIMEOUT: "120000",
        }
    )


_processes: dict[UUID, asyncio.subprocess.Process] = {}
_logger = logging.getLogger("bca.claude")


async def run_turn(
    binary: Path,
    profile: ClaudeProfile,
    mcp_config_path: Path,
    playbook_path: Path,
    settings_path: Path,
    brief: str,
    operator_message: str,
    model: str,
    effort: str = "high",
    max_turns: int = 25,
    session: SessionRef | None = None,
) -> AsyncIterator[Event]:
    if session is None:
        session = SessionRef(uuid4())
    command = build_turn_command(
        binary,
        profile.config_dir,
        mcp_config_path,
        playbook_path,
        settings_path,
        model,
        effort,
        max_turns,
        session,
    )
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=build_turn_env(profile),
        start_new_session=os.name != "nt",
    )
    _processes[session.session_id] = process
    assert process.stdin is not None and process.stdout is not None
    message = (
        json.dumps(
            {"type": "user", "message": {"role": "user", "content": brief + "\n\n" + operator_message}}
        )
        + "\n"
    )
    process.stdin.write(message.encode("utf-8"))
    await process.stdin.drain()
    process.stdin.close()
    try:
        first = True
        rate_guard = RateLimitGuard()
        async for raw in process.stdout:
            event = parse_line(raw.decode("utf-8", errors="replace"))
            if first and event.kind == "system":
                problems = check_init(event, model)
                if problems:
                    raise TurnGuardError("Claude init rejected: " + "; ".join(problems))
                first = False
            if event.kind == "rate_limit":
                decision = rate_guard.inspect(event)
                if decision.blocked_reason is not None:
                    raise TurnGuardError("Claude rate policy blocked turn: " + decision.blocked_reason)
            yield event
    except Exception:
        if process.returncode is None:
            _terminate_process_tree(process)
        raise
    finally:
        await process.wait()
        _processes.pop(session.session_id, None)


def stop_turn(session: SessionRef | UUID) -> None:
    process = _processes.get(session.session_id if isinstance(session, SessionRef) else session)
    if process is None or process.returncode is not None:
        return
    _terminate_process_tree(process)


def _terminate_process_tree(process: asyncio.subprocess.Process) -> None:
    if os.name == "nt":
        completed = subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True, check=False
        )
        if completed.returncode != 0:
            _logger.warning("taskkill failed for Claude process %s", process.pid)
    else:
        with contextlib.suppress(ProcessLookupError):
            cast(Any, os).killpg(process.pid, signal.SIGTERM)
