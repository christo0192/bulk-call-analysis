"""Synthetic Claude Code stream-json fake; it never contacts a service."""

from __future__ import annotations

import json
import os
import sys

scenario = os.environ.get("FAKE_CLAUDE_SCENARIO", "normal")
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
}
for event in payloads.get(scenario, payloads["normal"]):
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()
