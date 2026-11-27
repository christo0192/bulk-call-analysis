from __future__ import annotations

from bca.claude.stream import Event, Limited, RateLimitGuard, StreamParser, Warning, check_init, result_error
from bca.claude.version import PINNED_CLAUDE_VERSION


def good() -> Event:
    return Event(
        "system",
        {
            "type": "system",
            "subtype": "init",
            "apiKeySource": "none",
            "model": "m",
            "permissionMode": "dontAsk",
            "claude_code_version": PINNED_CLAUDE_VERSION,
            "fast_mode_state": "off",
            "tools": ["mcp__bq__x", "mcp__app__x", "EndConversation"],
            "mcp_servers": [{"name": "bq", "status": "connected"}, {"name": "app", "status": "connected"}],
            "plugins": [],
        },
    )


def test_documented_init_shape_and_guards() -> None:
    assert check_init(good(), "m") == []
    for key, value in (
        ("fast_mode_state", "on"),
        ("claude_code_version", "old"),
        ("mcp_servers", [{"name": "bq", "status": "failed"}]),
        ("tools", ["Bash"]),
        ("plugins", ["not-an-object"]),
    ):
        altered = dict(good().data)
        altered[key] = value
        assert check_init(Event("system", altered), "m")


def test_rate_limit_guard_uses_documented_nested_info_and_fails_closed() -> None:
    guard = RateLimitGuard()
    assert isinstance(
        guard.inspect(
            Event(
                "rate_limit",
                {
                    "rate_limit_info": {
                        "status": "rejected",
                        "resetsAt": "x",
                        "overageEnabled": False,
                    }
                },
            )
        ).status,
        Limited,
    )
    assert isinstance(
        guard.inspect(
            Event(
                "rate_limit",
                {
                    "rate_limit_info": {
                        "status": "allowed_warning",
                        "utilization": 80,
                        "overageEnabled": False,
                    }
                },
            )
        ).status,
        Warning,
    )
    assert (
        guard.inspect(
            Event(
                "rate_limit",
                {
                    "rate_limit_info": {
                        "status": "allowed",
                        "overageEnabled": True,
                    }
                },
            )
        ).blocked_reason
        == "extra_usage_on"
    )
    assert (
        guard.inspect(
            Event(
                "rate_limit",
                {
                    "rate_limit_info": {
                        "status": "allowed",
                        "errorCode": "credits_required",
                        "overageEnabled": False,
                    }
                },
            )
        ).status
        is not None
    )
    assert guard.inspect(Event("rate_limit", {})).blocked_reason is not None


def test_result_error_uses_documented_fields_and_declines_are_not_retryable() -> None:
    error = result_error(
        Event(
            "result",
            {
                "subtype": "success",
                "is_error": True,
                "stop_reason": "refusal",
                "result": "declined",
            },
        )
    )
    assert error is not None and error.category == "declined" and not error.retryable
    error = result_error(
        Event(
            "result",
            {
                "subtype": "error_max_turns",
                "is_error": True,
                "errors": ["max turns"],
            },
        )
    )
    assert error is not None and error.category == "max_turns"


def test_stream_event_and_content_blocks_are_mapped() -> None:
    parser = StreamParser()
    assert (
        parser.feed(
            '{"type":"stream_event","event":{"type":"content_block_delta","delta":{"text":"hi"}}}'
        ).kind
        == "assistant_delta"
    )
    assert parser.feed('{"type":"assistant","message":{"content":[{"type":"tool_use"}]}}').kind == "tool_use"
    assert parser.feed('{"type":"user","message":{"content":[{"type":"tool_result"}]}}').kind == "tool_result"
