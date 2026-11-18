from decimal import Decimal

from bca.luna.quota import ALLOW, INTERRUPT_ALL, STOP_DISPATCH, QuotaGuard, parse_rate_limits


def snapshot(percent: int, **kwargs: object):
    result = {
        "rateLimits": {
            "limitId": "codex",
            "primary": {"usedPercent": percent, "windowDurationMins": 60, "resetsAt": 10},
            "credits": {"balance": "5", "hasCredits": True},
        },
        "ordinaryUsageAllowed": True,
    }
    result.update(kwargs)
    return parse_rate_limits(result, "codex")


def test_quota_decisions() -> None:
    guard = QuotaGuard(95, Decimal("5"))
    assert guard.decide(snapshot(94)).kind == ALLOW
    assert guard.decide(snapshot(95)).kind == STOP_DISPATCH
    assert guard.decide(snapshot(100)).kind == INTERRUPT_ALL
    assert guard.decide(snapshot(94, ordinaryUsageAllowed=False)).kind == INTERRUPT_ALL


def test_bucket_selected_by_model() -> None:
    result = {
        "rateLimitsByLimitId": {
            "a": {"limitId": "a", "normalModelSlug": "other", "primary": {"usedPercent": 1}},
            "codex": {"limitId": "codex", "primary": {"usedPercent": 2}},
        }
    }
    assert parse_rate_limits(result, "other").limit_id == "a"
