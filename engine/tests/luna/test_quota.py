from decimal import Decimal
from typing import Any

import pytest

from bca.luna.quota import (
    ALLOW,
    INTERRUPT_ALL,
    STOP_DISPATCH,
    QuotaGuard,
    QuotaSnapshot,
    merge_update,
    parse_rate_limits,
)


def snapshot(percent: int | None = 0, **kwargs: object) -> QuotaSnapshot:
    primary = None if percent is None else {"usedPercent": percent, "windowDurationMins": 60, "resetsAt": 10}
    rate: dict[str, Any] = {"limitId": "codex", "primary": primary, "credits": {"balance": "5"}}
    rate.update({key: value for key, value in kwargs.items() if key != "ordinaryUsageAllowed"})
    result: dict[str, Any] = {
        "rateLimits": rate,
        "ordinaryUsageAllowed": kwargs.get("ordinaryUsageAllowed", True),
    }
    return parse_rate_limits(result, "codex")


@pytest.mark.parametrize(
    ("value", "extra", "expected"),
    [
        (94, {}, ALLOW),
        (95, {}, STOP_DISPATCH),
        (100, {}, INTERRUPT_ALL),
        (94, {"credits": {"balance": "4"}}, INTERRUPT_ALL),
        (94, {"spendControlReached": True}, INTERRUPT_ALL),
        (94, {"rateLimitReachedType": "daily"}, INTERRUPT_ALL),
        (94, {"ordinaryUsageAllowed": False}, INTERRUPT_ALL),
    ],
)
def test_quota_decisions(value: int, extra: dict[str, object], expected: str) -> None:
    assert QuotaGuard(95, Decimal("5")).decide(snapshot(value, **extra)).kind == expected


def test_null_windows_are_unknown_and_allow() -> None:
    assert QuotaGuard(95, Decimal("5")).decide(snapshot(None)).kind == ALLOW


def test_stop_resume_is_latest_reset() -> None:
    result = {
        "rateLimits": {
            "primary": {"usedPercent": 95, "resetsAt": 10},
            "secondary": {"usedPercent": 95, "resetsAt": 20},
        }
    }
    decision = QuotaGuard(95, None).decide(parse_rate_limits(result, "codex"))
    assert (decision.kind, decision.resume_at) == (STOP_DISPATCH, 20)


def test_bucket_selection_and_codex_fallback() -> None:
    result = {
        "rateLimitsByLimitId": {
            "a": {"limitId": "a", "normalModelSlug": "other", "primary": {"usedPercent": 1}},
            "codex": {"limitId": "codex", "primary": {"usedPercent": 2}},
        }
    }
    assert parse_rate_limits(result, "other").limit_id == "a"
    assert parse_rate_limits(result, "missing").limit_id == "codex"


@pytest.mark.parametrize(("percent", "expected"), [(94, True), (95, False)])
def test_can_resume(percent: int, expected: bool) -> None:
    assert QuotaGuard(95, Decimal("5")).can_resume(snapshot(percent), 95) is expected


def test_merge_update_account_rate_limits_payload() -> None:
    merged = merge_update(
        snapshot(20),
        {"rateLimits": {"primary": {"usedPercent": 96, "resetsAt": 42}, "credits": {"balance": "5"}}},
    )
    assert merged.primary is not None
    assert (merged.primary.used_percent, merged.primary.resets_at) == (96, 42)
