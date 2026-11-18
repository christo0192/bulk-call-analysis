from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
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
    # These names and nesting rules are from the Agent SDK TypeScript message
    # types: stream_event wraps a raw Anthropic event, while tool calls/results
    # are content blocks inside assistant/user messages.
    if raw_type == "stream_event":
        nested = value.get("event")
        if isinstance(nested, dict):
            nested_value = dict(nested)
            nested_value["_stream_event"] = value
            value = nested_value
            raw_type = str(value.get("type", ""))
    if raw_type == "system" and value.get("subtype") == "api_retry":
        raw_type = "api_retry"
    kinds: dict[str, EventKind] = {
        "system": "system",
        "init": "system",
        "content_block_delta": "assistant_delta",
        "assistant": "assistant",
        "rate_limit_event": "rate_limit",
        "rate_limit": "rate_limit",
        "api_retry": "api_retry",
        "result": "result",
    }
    kind = kinds.get(raw_type, "unknown")
    if raw_type in {"assistant", "user"}:
        content = (
            value.get("message", {}).get("content", []) if isinstance(value.get("message"), dict) else []
        )
        if isinstance(content, list):
            if any(isinstance(block, dict) and block.get("type") == "tool_use" for block in content):
                kind = "tool_use"
            elif any(isinstance(block, dict) and block.get("type") == "tool_result" for block in content):
                kind = "tool_result"
    return Event(kind, value)


class StreamParser:
    """Parse stream-json lines and combine adjacent partial assistant deltas."""

    def __init__(self) -> None:
        self.bad_lines = 0
        self._partial = ""

    def feed(self, line: str) -> Event:
        event = parse_line(line)
        if event.kind == "unknown" and event.data.get("bad") is True:
            self.bad_lines += 1
        if event.kind == "assistant_delta":
            delta = event.data.get("delta", event.data.get("text", ""))
            if isinstance(delta, dict):
                delta = delta.get("text", "")
            if isinstance(delta, str):
                self._partial += delta
                data = dict(event.data)
                data["assembled"] = self._partial
                return Event("assistant_delta", data)
        return event


def parse_stream(lines: Iterable[str]) -> Iterator[Event]:
    parser = StreamParser()
    for line in lines:
        yield parser.feed(line)


def check_init(init: Event, expected: str) -> list[str]:
    data = init.data
    problems: list[str] = []
    if data.get("apiKeySource") != "none":
        problems.append("apiKeySource is not none")
    if data.get("model") != expected:
        problems.append("unexpected model")
    if data.get("permissionMode") != "dontAsk":
        problems.append("permission mode is not dontAsk")
    if init.kind != "system" or data.get("subtype") != "init":
        problems.append("first event is not system/init")
    if data.get("claude_code_version") != "2.1.327":
        problems.append("unexpected Claude Code version")
    if data.get("fast_mode_state", "off") != "off":
        problems.append("fast mode is enabled")
    tools = data.get("tools", [])
    if not isinstance(tools, list) or any(
        not (
            isinstance(t, str)
            and (t == "EndConversation" or t.startswith("mcp__bq__") or t.startswith("mcp__app__"))
        )
        for t in tools
    ):
        problems.append("tool list contains a non-approved tool")
    servers = data.get("mcp_servers")
    names = (
        {item.get("name") for item in servers}
        if isinstance(servers, list) and all(isinstance(item, dict) for item in servers)
        else set()
    )
    connected = isinstance(servers, list) and all(
        item.get("status") == "connected" for item in servers if isinstance(item, dict)
    )
    if names != {"bq", "app"}:
        problems.append("MCP servers are not exactly bq and app")
    if not connected:
        problems.append("an MCP server is not connected")
    if data.get("skills", data.get("loadedSkills", [])):
        problems.append("skills were loaded")
    plugins = data.get("plugins", data.get("loadedPlugins", []))
    if not isinstance(plugins, list) or any(
        not isinstance(plugin, dict)
        or not isinstance(plugin.get("name"), str)
        or not isinstance(plugin.get("path"), str)
        for plugin in plugins
    ):
        problems.append("plugins have an invalid shape")
    elif plugins:
        problems.append("plugins were loaded")
    return problems


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
        # SDKRateLimitEvent documents rate_limit_info as the sole container for
        # status, resetsAt, utilization, overageEnabled, and errorCode.
        info = event.data.get("rate_limit_info")
        if not isinstance(info, dict):
            return RateLimitDecision(blocked_reason="unknown_rate_limit_shape")
        if info.get("overageEnabled") is True:
            return RateLimitDecision(blocked_reason="extra_usage_on")
        status = info.get("status")
        if status == "rejected" or info.get("errorCode") == "credits_required":
            return RateLimitDecision(Limited(info.get("resetsAt")), "rate_limit_rejected")
        if status == "allowed_warning":
            return RateLimitDecision(Warning(info.get("utilization")))
        if status == "allowed":
            return RateLimitDecision()
        return RateLimitDecision(blocked_reason="unknown_rate_limit_status")


@dataclass(frozen=True)
class TurnError:
    category: Literal["auth", "limited", "declined", "max_turns", "missing_session", "other"]
    message: str

    @property
    def retryable(self) -> bool:
        return self.category != "declined"


def result_error(event: Event) -> TurnError | None:
    if event.kind != "result" or not event.data.get("is_error", event.data.get("isError", False)):
        return None
    subtype = event.data.get("subtype")
    errors = event.data.get("errors")
    message = (
        "; ".join(str(item) for item in errors)
        if isinstance(errors, list)
        else str(event.data.get("result", "Claude turn failed"))
    )
    stop_reason = event.data.get("stop_reason")
    terminal_reason = event.data.get("terminal_reason")
    api_status = event.data.get("api_error_status")
    lower = message.lower()
    category: Literal["auth", "limited", "declined", "max_turns", "missing_session", "other"] = "other"
    if stop_reason == "refusal" or terminal_reason == "refusal":
        category = "declined"
    elif subtype == "error_max_turns":
        category = "max_turns"
    elif api_status in {401, 403}:
        category = "auth"
    elif api_status == 429 or "credits_required" in lower:
        category = "limited"
    elif "session not found" in lower or "conversation not found" in lower:
        category = "missing_session"
    elif "auth" in lower or "login" in lower:
        category = "auth"
    elif "limit" in lower or "rate limit" in lower:
        category = "limited"
    elif "declin" in lower or "safety" in lower:
        category = "declined"
    elif subtype == "error_during_execution" and "max turn" in lower:
        category = "max_turns"
    return TurnError(category, message)
