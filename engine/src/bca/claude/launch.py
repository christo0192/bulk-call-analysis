from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import signal
import subprocess
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

from bca.childenv import build_child_env

from .profile import ENV_DISABLE_MEMORY, ClaudeProfile
from .stream import Event, Limited, RateLimitGuard, TurnError, check_init, parse_line, result_error
from .version import PINNED_CLAUDE_VERSION as PINNED_CLAUDE_VERSION
from .winjob import assign_kill_on_close, close_job, terminate_job

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
ENV_MCP_SERVERS = "ENABLE_CLAUDEAI_MCP_SERVERS"
ENV_DISABLE_FAST = "CLAUDE_CODE_DISABLE_FAST_MODE"
ENV_DISABLE_WORKFLOWS = "CLAUDE_CODE_DISABLE_WORKFLOWS"
ENV_DISABLE_MDS = "CLAUDE_CODE_DISABLE_CLAUDE_MDS"
ALLOWED_EFFORTS = {"low", "medium", "high"}


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

    def __init__(self, message: str, reason: str | None = None, resets_at: object = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.resets_at = resets_at


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
    if not re.fullmatch(r"claude-[a-z0-9][a-z0-9.-]*", model) or model.endswith("plan"):
        raise ValueError("Claude model must be a fully qualified pinned subscription model name")
    if effort not in ALLOWED_EFFORTS:
        raise ValueError("unsupported effort")
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
    isolated_anthropic = (config_dir / "anthropic").resolve()
    isolated_anthropic.mkdir(parents=True, exist_ok=True)
    return build_child_env(
        {
            ENV_CONFIG: str(config_dir),
            ENV_DISABLE_MEMORY: "1",
            ENV_DISABLE_UPDATE: "1",
            ENV_NONESSENTIAL: "1",
            ENV_TOOL_SEARCH: "false",
            ENV_MCP_SERVERS: "false",
            ENV_DISABLE_FAST: "1",
            ENV_DISABLE_WORKFLOWS: "1",
            ENV_DISABLE_MDS: "1",
            ENV_MCP_TIMEOUT: "120000",
            "APPDATA": str(isolated_anthropic),
            "XDG_CONFIG_HOME": str(isolated_anthropic),
        }
    )


_processes: dict[UUID, asyncio.subprocess.Process] = {}
_jobs: dict[UUID, object | None] = {}
_termination_tasks: set[asyncio.Task[None]] = set()
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
    from .auth import auth_status

    if not (await asyncio.to_thread(auth_status, binary, profile)).ok:
        raise TurnGuardError("Claude subscription authentication is not active")
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
        cwd=str(profile.project_workdir(str(session.session_id))),
        start_new_session=os.name != "nt",
    )
    job = assign_kill_on_close(process.pid) if os.name == "nt" else None
    _processes[session.session_id] = process
    _jobs[session.session_id] = job
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
    completed = False
    stderr_task = asyncio.create_task(_drain_stderr(process.stderr))
    try:
        first = True
        rate_guard = RateLimitGuard()
        async for raw in process.stdout:
            event = parse_line(raw.decode("utf-8", errors="replace"))
            if first and not (event.kind == "system" and event.data.get("subtype") == "init"):
                raise TurnGuardError("Claude did not begin with system/init")
            if first:
                problems = check_init(event, model)
                if problems:
                    raise TurnGuardError("Claude init rejected: " + "; ".join(problems))
                first = False
            if event.kind == "system" and event.data.get("subtype") == "model_refusal_fallback":
                raise TurnGuardError("Claude attempted a safety model fallback")
            if event.kind == "assistant":
                message_data = event.data.get("message")
                if isinstance(message_data, dict) and message_data.get("model") != model:
                    raise TurnGuardError("Claude assistant used an unexpected model")
            if event.kind == "result":
                usage = event.data.get("modelUsage")
                if isinstance(usage, dict) and set(usage) != {model}:
                    raise TurnGuardError("Claude result used an unexpected model")
                failure = result_error(event)
                if failure:
                    raise TurnGuardError(f"Claude turn failed: {failure.category}: {failure.message}")
            if event.kind == "rate_limit":
                decision = rate_guard.inspect(event)
                if decision.blocked_reason is not None or isinstance(decision.status, Limited):
                    # Let the caller persist the event before enforcing the
                    # policy.  Rate-limit observations are read-model data,
                    # including rejected events that terminate the turn.
                    yield event
                    reason = decision.blocked_reason or "limited"
                    if reason == "extra_usage_on":
                        raise TurnGuardError("Claude rate policy blocked turn: extra_usage_on", reason)
                    if reason not in {"rate_limit_rejected", "limited"}:
                        raise TurnGuardError("Claude rate policy blocked turn: turn_failed", reason)
                    resets_at = decision.status.resets_at if isinstance(decision.status, Limited) else None
                    raise TurnGuardError(
                        "Claude rate policy blocked turn: rate_limit_rejected",
                        "rate_limit_rejected",
                        resets_at,
                    )
            yield event
        await process.wait()
        stderr_tail = await stderr_task
        if first and "No conversation found with session ID" in stderr_tail:
            raise TurnGuardError("No conversation found with session ID")
        if first:
            raise TurnGuardError("Claude exited without system/init")
        if process.returncode != 0 and "No conversation found with session ID" in stderr_tail:
            raise TurnGuardError("No conversation found with session ID")
        completed = True
    except BaseException:
        if process.returncode is None:
            await _terminate_process_tree(process, job)
        raise
    finally:
        if not completed and process.returncode is None:
            await _terminate_process_tree(process, job)
        if process.returncode is None:
            await _terminate_process_tree(process, job)
        try:
            await asyncio.wait_for(asyncio.shield(process.wait()), timeout=2.0)
        except TimeoutError as error:
            raise RuntimeError("Claude process did not exit after termination") from error
        stderr_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stderr_task
        close_job(job)
        _processes.pop(session.session_id, None)
        _jobs.pop(session.session_id, None)


def stop_turn(session: SessionRef | UUID) -> None:
    process = _processes.get(session.session_id if isinstance(session, SessionRef) else session)
    if process is None or process.returncode is not None:
        return
    process_id = session.session_id if isinstance(session, SessionRef) else session
    task = asyncio.create_task(_terminate_process_tree(process, _jobs.get(process_id)))
    _termination_tasks.add(task)
    task.add_done_callback(_termination_tasks.discard)


async def _drain_stderr(stderr: asyncio.StreamReader | None) -> str:
    tail = bytearray()
    if stderr is not None:
        while chunk := await stderr.read(65536):
            tail.extend(chunk)
            del tail[:-65536]
    return tail.decode("utf-8", errors="replace")


async def _terminate_process_tree(process: asyncio.subprocess.Process, job: object | None) -> None:
    if os.name == "nt":
        if job is None or not terminate_job(job):
            try:
                completed = subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                    capture_output=True,
                    check=False,
                    timeout=2.0,
                    env=build_child_env({}),
                )
            except subprocess.TimeoutExpired:
                completed = None
            if completed is None:
                _logger.warning("taskkill failed for Claude process %s", process.pid)
            elif completed.returncode == 128:
                _logger.debug("Claude process %s was already gone when taskkill ran", process.pid)
            elif completed.returncode != 0:
                _logger.warning(
                    "taskkill failed for Claude process %s (exit %s)", process.pid, completed.returncode
                )
        try:
            await asyncio.wait_for(asyncio.shield(process.wait()), timeout=2.0)
        except TimeoutError:
            process.kill()
            try:
                await asyncio.wait_for(asyncio.shield(process.wait()), timeout=2.0)
            except TimeoutError as error:
                raise RuntimeError("Claude process could not be killed") from error
    else:
        try:
            cast(Any, os).killpg(process.pid, signal.SIGTERM)
        except (PermissionError, ProcessLookupError) as error:
            _logger.debug(
                "Claude process group %s was already gone or not signalable: %s", process.pid, error
            )
        try:
            await asyncio.wait_for(asyncio.shield(process.wait()), timeout=1.0)
        except TimeoutError:
            try:
                cast(Any, os).killpg(process.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
            except (PermissionError, ProcessLookupError) as error:
                _logger.debug(
                    "Claude process group %s was already gone or not signalable: %s", process.pid, error
                )
            try:
                await asyncio.wait_for(asyncio.shield(process.wait()), timeout=2.0)
            except TimeoutError as error:
                raise RuntimeError("Claude process could not be killed") from error
        try:
            cast(Any, os).killpg(process.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        except (PermissionError, ProcessLookupError) as error:
            _logger.debug(
                "Claude process group %s was already gone or not signalable: %s", process.pid, error
            )
