"""Synthetic Claude Code stream-json fake; it never contacts a service."""

from __future__ import annotations

import json
import os
import sys
import time

scenario = os.environ.get("FAKE_CLAUDE_SCENARIO", "normal")
stdin_log = os.environ.get("FAKE_CLAUDE_STDIN_LOG")
if stdin_log:
    with open(stdin_log, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(sys.stdin.read())
else:
    sys.stdin.read()
if scenario == "spawn_child":
    import subprocess

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    with open(os.environ["FAKE_CLAUDE_CHILD_PID"], "w", encoding="utf-8", newline="\n") as handle:
        handle.write(str(child.pid))
    time.sleep(120)
payloads: dict[str, list[dict[str, object]]] = {
    "normal": [
        {
            "type": "system",
            "apiKeySource": "none",
            "model": "claude-opus-5-5",
            "permissionMode": "dontAsk",
            "tools": ["mcp__bq__query"],
            "mcpServers": {"bq": {"connected": True}, "app": {"connected": True}},
        },
        {"type": "result", "is_error": False},
    ],
    "decline": [{"type": "result", "is_error": True, "error": "safety decline"}],
    "rate_limit": [{"type": "rate_limit_event", "status": "rejected", "resetsAt": "later"}],
    "bad_init": [
        {
            "type": "system",
            "apiKeySource": "key",
            "model": "claude-opus-5-5",
            "permissionMode": "dontAsk",
            "tools": ["mcp__bq__query"],
            "mcpServers": {"bq": {"connected": True}, "app": {"connected": True}},
        }
    ],
}
for event in payloads.get(scenario, payloads["normal"]):
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()
