from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

EventKind = Literal[
    "system",
    "assistant_delta",
    "assistant",
    "tool_use",
    "tool_result",
    "rate_limit",
    "api_retry",
    "result",
    "unknown",
]


@dataclass(frozen=True)
class Event:
    kind: EventKind
    data: dict[str, Any] = field(default_factory=dict)


SystemInit = Event
AssistantDelta = Event
AssistantMessage = Event
ToolUse = Event
ToolResult = Event
RateLimitEvent = Event
ApiRetry = Event
Result = Event
Unknown = Event


def parse_line(line: str) -> Event:
    try:
        value = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return Event("unknown", {"raw": line, "bad": True})
    if not isinstance(value, dict):
        return Event("unknown", {"value": value})
    raw_type = str(value.get("type", ""))
    kinds: dict[str, EventKind] = {
        "system": "system",
        "init": "system",
        "assistant_delta": "assistant_delta",
        "content_block_delta": "assistant_delta",
        "assistant": "assistant",
        "tool_use": "tool_use",
        "tool_result": "tool_result",
        "rate_limit_event": "rate_limit",
        "rate_limit": "rate_limit",
        "api_retry": "api_retry",
        "result": "result",
    }
    return Event(kinds.get(raw_type, "unknown"), value)


def check_init(init: Event, expected: str) -> list[str]:
    data = init.data
    problems: list[str] = []
    if data.get("apiKeySource") != "none":
        problems.append("apiKeySource is not none")
    if data.get("model") != expected:
        problems.append("unexpected model")
    if data.get("permissionMode") != "dontAsk":
        problems.append("permission mode is not dontAsk")
    tools = data.get("tools", [])
    if not isinstance(tools, list) or any(
        not (isinstance(t, str) and (t.startswith("mcp__bq__") or t.startswith("mcp__app__"))) for t in tools
    ):
        problems.append("tool list contains a non-approved tool")
    servers = data.get("mcpServers", data.get("mcp_servers", {}))
    if isinstance(servers, dict):
        names = set(servers)
        connected = all(
            (v.get("connected") is True if isinstance(v, dict) else v is True) for v in servers.values()
        )
    else:
        names = set()
        connected = False
    if names != {"bq", "app"}:
        problems.append("MCP servers are not exactly bq and app")
    if not connected:
        problems.append("an MCP server is not connected")
    if data.get("skills", data.get("loadedSkills", [])):
        problems.append("skills were loaded")
    if data.get("plugins", data.get("loadedPlugins", [])):
        problems.append("plugins were loaded")
    return problems


RATE_FIELDS = {
    "status": ("status", "rateLimitStatus"),
    "reset": ("resetsAt", "resetAt"),
    "utilization": ("utilization", "utilizationPercent"),
    "using_overage": ("isUsingOverage",),
    "overage_enabled": ("overageEnabled",),
}


@dataclass(frozen=True)
class Limited:
    resets_at: datetime | str | None


@dataclass(frozen=True)
class Warning:
    utilization: float | int | None


@dataclass(frozen=True)
class RateLimitDecision:
    status: Limited | Warning | None = None
    blocked_reason: str | None = None


class RateLimitGuard:
    def inspect(self, event: Event) -> RateLimitDecision:
        data = event.data
        if any(bool(data.get(k)) for k in RATE_FIELDS["using_overage"]):
            return RateLimitDecision(blocked_reason="overage_in_use")
        if any(bool(data.get(k)) for k in RATE_FIELDS["overage_enabled"]):
            return RateLimitDecision(blocked_reason="extra_usage_on")
        status = next((data.get(k) for k in RATE_FIELDS["status"]), None)
        if status == "rejected":
            return RateLimitDecision(Limited(next((data.get(k) for k in RATE_FIELDS["reset"]), None)))
        if status == "allowed_warning":
            return RateLimitDecision(Warning(next((data.get(k) for k in RATE_FIELDS["utilization"]), None)))
        return RateLimitDecision()


@dataclass(frozen=True)
class TurnError:
    category: Literal["auth", "limited", "declined", "max_turns", "other"]
    message: str


def result_error(event: Event) -> TurnError | None:
    if event.kind != "result" or not event.data.get("is_error", event.data.get("isError", False)):
        return None
    message = str(event.data.get("error", event.data.get("message", "Claude turn failed")))
    lower = message.lower()
    category: Literal["auth", "limited", "declined", "max_turns", "other"] = "other"
    if "auth" in lower or "login" in lower:
        category = "auth"
    elif "limit" in lower or "rate" in lower:
        category = "limited"
    elif "declin" in lower or "safety" in lower:
        category = "declined"
    elif "max" in lower and "turn" in lower:
        category = "max_turns"
    return TurnError(category, message)
