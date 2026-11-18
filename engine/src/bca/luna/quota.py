from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any


@dataclass(frozen=True)
class Window:
    used_percent: int
    window_minutes: int | None
    resets_at: int | None


@dataclass(frozen=True)
class QuotaSnapshot:
    limit_id: str | None
    primary: Window | None
    secondary: Window | None
    credits_balance: Decimal | None
    has_credits: bool
    spend_control_reached: bool | None
    rate_limit_reached_type: str | None
    ordinary_usage_allowed: bool
    plan_type: str | None
    taken_at: int


@dataclass(frozen=True)
class Decision:
    kind: str
    reason: str
    resume_at: int | None


ALLOW, STOP_DISPATCH, INTERRUPT_ALL = "ALLOW", "STOP_DISPATCH", "INTERRUPT_ALL"


def _window(value: Any) -> Window | None:
    if not isinstance(value, dict):
        return None
    return Window(int(value.get("usedPercent", 0)), value.get("windowDurationMins"), value.get("resetsAt"))


def _snapshot(rate: dict[str, Any], top: dict[str, Any], taken_at: int) -> QuotaSnapshot:
    credits = rate.get("credits") or {}
    balance = credits.get("balance")
    try:
        parsed_balance = Decimal(str(balance)) if balance is not None else None
    except InvalidOperation:
        parsed_balance = None
    return QuotaSnapshot(
        rate.get("limitId"),
        _window(rate.get("primary")),
        _window(rate.get("secondary")),
        parsed_balance,
        bool(credits.get("hasCredits", False)),
        rate.get("spendControlReached"),
        rate.get("rateLimitReachedType"),
        bool(top.get("ordinaryUsageAllowed", True)),
        rate.get("planType"),
        taken_at,
    )


def parse_rate_limits(result: dict[str, Any], model: str) -> QuotaSnapshot:
    buckets = result.get("rateLimitsByLimitId") or {}
    selected = next(
        (v for v in buckets.values() if isinstance(v, dict) and v.get("normalModelSlug") == model), None
    )
    selected = selected or buckets.get("codex") or result.get("rateLimits") or {}
    return _snapshot(selected, result, int(datetime.now(UTC).timestamp()))


def merge_update(snapshot: QuotaSnapshot, notification_params: dict[str, Any]) -> QuotaSnapshot:
    update = notification_params.get("rateLimits", notification_params)
    if not isinstance(update, dict):
        return snapshot
    primary = update.get("primary")
    if primary is None and snapshot.primary is not None:
        primary = {
            "usedPercent": snapshot.primary.used_percent,
            "windowDurationMins": snapshot.primary.window_minutes,
            "resetsAt": snapshot.primary.resets_at,
        }
    secondary = update.get("secondary")
    if secondary is None and snapshot.secondary is not None:
        secondary = {
            "usedPercent": snapshot.secondary.used_percent,
            "windowDurationMins": snapshot.secondary.window_minutes,
            "resetsAt": snapshot.secondary.resets_at,
        }
    credits = update.get("credits")
    if credits is None:
        credits = {
            "balance": str(snapshot.credits_balance) if snapshot.credits_balance is not None else None,
            "hasCredits": snapshot.has_credits,
        }
    top = {"ordinaryUsageAllowed": update.get("ordinaryUsageAllowed", snapshot.ordinary_usage_allowed)}
    merged = {
        "limitId": snapshot.limit_id,
        "primary": primary,
        "secondary": secondary,
        "credits": credits,
        "spendControlReached": update.get("spendControlReached", snapshot.spend_control_reached),
        "rateLimitReachedType": update.get("rateLimitReachedType", snapshot.rate_limit_reached_type),
        "planType": update.get("planType", snapshot.plan_type),
    }
    result = _snapshot(merged, top, int(datetime.now(UTC).timestamp()))
    return result


class QuotaGuard:
    def __init__(self, stop_percent: int, baseline_balance: Decimal | None) -> None:
        self.stop_percent, self.baseline_balance = stop_percent, baseline_balance

    def _windows(self, s: QuotaSnapshot) -> list[Window]:
        return [w for w in (s.primary, s.secondary) if w is not None]

    def decide(self, s: QuotaSnapshot) -> Decision:
        windows = self._windows(s)
        resume = max(
            (w.resets_at for w in windows if w.used_percent >= self.stop_percent and w.resets_at is not None),
            default=None,
        )
        credit_drop = (
            self.baseline_balance is not None
            and s.credits_balance is not None
            and s.credits_balance < self.baseline_balance
        )
        if credit_drop:
            return Decision(INTERRUPT_ALL, "credit balance dropped below baseline", resume)
        if s.spend_control_reached or s.rate_limit_reached_type is not None or not s.ordinary_usage_allowed:
            return Decision(INTERRUPT_ALL, "subscription usage is unavailable", resume)
        if any(w.used_percent >= 100 for w in windows):
            return Decision(INTERRUPT_ALL, "a rate-limit window is exhausted", resume)
        if any(w.used_percent >= self.stop_percent for w in windows):
            return Decision(STOP_DISPATCH, "rate-limit stop threshold reached", resume)
        return Decision(ALLOW, "quota is available", resume)

    def can_resume(self, s: QuotaSnapshot, stop_percent: int) -> bool:
        return (
            s.ordinary_usage_allowed
            and s.rate_limit_reached_type is None
            and not s.spend_control_reached
            and all(w.used_percent < stop_percent for w in self._windows(s))
            and not (
                self.baseline_balance is not None
                and s.credits_balance is not None
                and s.credits_balance < self.baseline_balance
            )
        )

    def projected_leads_left(self, s: QuotaSnapshot, pct_per_lead: float | None) -> int | None:
        if pct_per_lead is None or pct_per_lead <= 0:
            return None
        headroom = min((100 - w.used_percent for w in self._windows(s)), default=100)
        return max(0, int(headroom // pct_per_lead))
