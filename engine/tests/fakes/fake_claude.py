"""Synthetic child using the documented Claude Code stream-json shapes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

if len(sys.argv) > 1 and sys.argv[1:4] == ["auth", "status", "--json"]:
    print(json.dumps({"loggedIn": True, "authMethod": "claude.ai", "configDirectory": "isolated"}))
    raise SystemExit(0)

scenario = os.environ.get("FAKE_CLAUDE_SCENARIO", "normal")
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
for event in payloads.get(scenario, payloads["normal"]):
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()
