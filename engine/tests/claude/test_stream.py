from __future__ import annotations

import pytest

from bca.claude.stream import (
    Event,
    Limited,
    RateLimitGuard,
    StreamParser,
    Warning,
    check_init,
    result_error,
)


def good() -> Event:
    return Event(
        "system",
        {
            "apiKeySource": "none",
            "model": "m",
            "permissionMode": "dontAsk",
            "tools": ["mcp__bq__x", "mcp__app__x"],
            "mcpServers": {"bq": {"connected": True}, "app": {"connected": True}},
        },
    )


def test_every_init_guard_failure_is_reported() -> None:
    data = good().data
    for key, value, text in (
        ("apiKeySource", "key", "apiKeySource"),
        ("model", "wrong", "model"),
        ("permissionMode", "ask", "permission"),
        ("tools", ["Bash"], "tool"),
        ("mcpServers", {"bq": {"connected": True}}, "MCP"),
        ("skills", ["x"], "skills"),
        ("plugins", ["x"], "plugins"),
        (
            "mcpServers",
            {"bq": {"connected": True}, "app": {"connected": True}, "extra": {"connected": True}},
            "MCP",
        ),
        ("mcpServers", {"bq": {"connected": True}, "app": {"connected": False}}, "connected"),
    ):
        altered = dict(data)
        altered[key] = value
        assert any(text in problem for problem in check_init(Event("system", altered), "m"))
    assert check_init(good(), "m") == []


def test_rate_limit_overage_and_decline() -> None:
    guard = RateLimitGuard()
    assert guard.inspect(Event("rate_limit", {"status": "rejected", "resetsAt": "x"})).status is not None
    assert (
        guard.inspect(Event("rate_limit", {"status": "allowed_warning", "utilization": 80})).status
        is not None
    )
    assert guard.inspect(Event("rate_limit", {"isUsingOverage": True})).blocked_reason == "overage_in_use"
    assert guard.inspect(Event("rate_limit", {"overageEnabled": True})).blocked_reason == "extra_usage_on"
    limited = guard.inspect(Event("rate_limit", {"status": "rejected", "resetsAt": "x"})).status
    warning = guard.inspect(Event("rate_limit", {"status": "allowed_warning", "utilization": 80})).status
    assert isinstance(limited, Limited) and limited.resets_at == "x"
    assert isinstance(warning, Warning) and warning.utilization == 80
    assert guard.inspect(Event("rate_limit", {"unexpected": object()})).blocked_reason is None


@pytest.mark.parametrize(
    ("message", "category"),
    [
        ("auth failed", "auth"),
        ("rate limit", "limited"),
        ("safety decline", "declined"),
        ("max turns reached", "max_turns"),
        ("broken", "other"),
    ],
)
def test_result_error_mapping(message: str, category: str) -> None:
    error = result_error(Event("result", {"is_error": True, "error": message}))
    assert error is not None and error.category == category
    assert error.retryable is not (category == "declined")


def test_bad_stream_line_is_unknown() -> None:
    parser = StreamParser()
    assert parser.feed("not json").kind == "unknown"
    assert parser.bad_lines == 1


def test_stream_parser_maps_all_events_and_assembles_deltas() -> None:
    parser = StreamParser()
    lines = [
        '{"type":"system"}',
        '{"type":"assistant_delta","delta":"hel"}',
        '{"type":"content_block_delta","delta":{"text":"lo"}}',
        '{"type":"assistant"}',
        '{"type":"tool_use"}',
        '{"type":"tool_result"}',
        '{"type":"rate_limit_event"}',
        '{"type":"api_retry"}',
        '{"type":"result"}',
        '{"type":"future"}',
    ]
    events = [parser.feed(line) for line in lines]
    assert [event.kind for event in events] == [
        "system",
        "assistant_delta",
        "assistant_delta",
        "assistant",
        "tool_use",
        "tool_result",
        "rate_limit",
        "api_retry",
        "result",
        "unknown",
    ]
    assert events[2].data["assembled"] == "hello"
