from __future__ import annotations

from bca.claude.stream import Event, RateLimitGuard, check_init, parse_line, result_error


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
    ):
        altered = dict(data)
        altered[key] = value
        assert any(text in problem for problem in check_init(Event("system", altered), "m"))


def test_rate_limit_overage_and_decline() -> None:
    guard = RateLimitGuard()
    assert guard.inspect(Event("rate_limit", {"status": "rejected", "resetsAt": "x"})).status is not None
    assert (
        guard.inspect(Event("rate_limit", {"status": "allowed_warning", "utilization": 80})).status
        is not None
    )
    assert guard.inspect(Event("rate_limit", {"isUsingOverage": True})).blocked_reason == "overage_in_use"
    assert guard.inspect(Event("rate_limit", {"overageEnabled": True})).blocked_reason == "extra_usage_on"
    assert result_error(Event("result", {"is_error": True, "error": "safety decline"})).category == "declined"  # type: ignore[union-attr]


def test_bad_stream_line_is_unknown() -> None:
    assert parse_line("not json").kind == "unknown"
