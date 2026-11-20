"""Synthetic child using the documented Claude Code stream-json shapes."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path


async def _call_scripted_tool() -> object:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    config = Path(sys.argv[sys.argv.index("--mcp-config") + 1])
    settings = json.loads(config.read_text(encoding="utf-8"))
    server = settings["mcpServers"]["bq"]
    parameters = StdioServerParameters(
        command=str(server["command"]), args=list(server["args"]), env=dict(server.get("env", {}))
    )
    async with (
        stdio_client(parameters) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        result = await session.call_tool("bq_query", {"sql": "SELECT 1"})
        return result.model_dump(mode="json")


if len(sys.argv) > 1 and sys.argv[1:4] == ["auth", "status", "--json"]:
    print(json.dumps({"loggedIn": True, "authMethod": "claude.ai", "configDirectory": "isolated"}))
    raise SystemExit(0)

scenario = os.environ.get("FAKE_CLAUDE_SCENARIO", "normal")
delay = float(os.environ.get("FAKE_CLAUDE_DELAY", "0"))
argv_log = os.environ.get("FAKE_CLAUDE_ARGV_LOG")
env_log = os.environ.get("FAKE_CLAUDE_ENV_LOG")
if argv_log:
    with open(argv_log, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(sys.argv, handle)
if env_log:
    with open(env_log, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(sorted(os.environ), handle)
stdin_log = os.environ.get("FAKE_CLAUDE_STDIN_LOG")
incoming = sys.stdin.read()
if stdin_log:
    with open(stdin_log, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(incoming)

init: dict[str, object] = {
    "type": "system",
    "subtype": "init",
    "apiKeySource": "none",
    "model": "claude-opus-5-5",
    "permissionMode": "dontAsk",
    "tools": ["mcp__bq__query", "mcp__app__query", "EndConversation"],
    "mcp_servers": [{"name": "bq", "status": "connected"}, {"name": "app", "status": "connected"}],
    "plugins": [],
    "fast_mode_state": "off",
    "claude_code_version": "2.1.327",
}
if scenario == "spawn_child":
    sys.stdout.write(json.dumps(init) + "\n")
    sys.stdout.flush()
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    with open(os.environ["FAKE_CLAUDE_CHILD_PID"], "w", encoding="utf-8", newline="\n") as handle:
        handle.write(str(child.pid))
    time.sleep(120)
if scenario == "hang":
    pid_file = os.environ.get("FAKE_CLAUDE_PID_FILE")
    if pid_file:
        with open(pid_file, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(str(os.getpid()))
    sys.stdout.write(json.dumps(init) + "\n")
    sys.stdout.flush()
    time.sleep(120)

if delay > 0:
    time.sleep(delay)

scripted_tool_result: object | None = None
if scenario == "tool_call":
    scripted_tool_result = asyncio.run(_call_scripted_tool())

payloads: dict[str, list[dict[str, object]]] = {
    "normal": [
        init,
        {"type": "result", "subtype": "success", "is_error": False, "modelUsage": {"claude-opus-5-5": {}}},
    ],
    "decline": [
        init,
        {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "stop_reason": "refusal",
            "result": "declined",
        },
    ],
    "rate_limit": [
        init,
        {
            "type": "rate_limit_event",
            "rate_limit_info": {
                "status": "rejected",
                "resetsAt": "later",
                "overageEnabled": False,
                "errorCode": "credits_required",
            },
        },
    ],
    "bad_init": [{**init, "apiKeySource": "key"}],
    "fallback": [
        init,
        {
            "type": "system",
            "subtype": "model_refusal_fallback",
            "original_model": "claude-opus-5-5",
            "fallback_model": "claude-opus-4-8",
        },
    ],
}
if scenario == "tool_call":
    payloads["tool_call"] = [
        init,
        {
            "type": "assistant",
            "message": {
                "model": "claude-opus-5-5",
                "content": [{"type": "tool_use", "name": "mcp__bq__bq_query", "input": {"sql": "SELECT 1"}}],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "content": scripted_tool_result}],
            },
        },
        {"type": "result", "subtype": "success", "is_error": False, "modelUsage": {"claude-opus-5-5": {}}},
    ]
if scenario == "missing_conversation" and "--resume" in sys.argv:
    selected_payloads = [
        init,
        {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "result": "No conversation found with session ID",
        },
    ]
else:
    selected_payloads = payloads.get(scenario, payloads["normal"])
for event in selected_payloads:
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()
