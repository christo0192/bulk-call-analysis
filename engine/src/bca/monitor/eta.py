from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import pairwise
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float: ...
    def monotonic(self) -> float: ...


class QuotaPoint(Protocol):
    used_percent: int
    resets_at: int | None
    taken_at: float


@dataclass(frozen=True)
class Eta:
    low: float
    high: float
    seeded: bool = False
    will_pause_at_fraction: float | None = None
    resume_at: float | None = None

    @property
    def seconds(self) -> float:
        return self.high

    def public(self) -> dict[str, object]:
        return {
            "low": self.low if math.isfinite(self.low) else None,
            "high": self.high if math.isfinite(self.high) else None,
            "seeded": self.seeded,
            "known": math.isfinite(self.low) and math.isfinite(self.high),
        }


class EtaEstimator:
    def __init__(self, window_s: float = 1200) -> None:
        if window_s <= 0:
            raise ValueError("window_s must be positive")
        self.window_s = window_s
        self._completed: list[float] = []
        self._paused: list[tuple[float, float]] = []
        self._sleep: list[tuple[float, float]] = []

    def completed(self, ts: float) -> None:
        self._completed.append(ts)

    def pause(self, start: float, end: float) -> None:
        self._paused.append((start, end))

    def sleep_gap(self, start: float, end: float) -> None:
        self._sleep.append((start, end))

    def _excluded(self, start: float, end: float) -> float:
        spans = sorted((a, b) for a, b in (*self._paused, *self._sleep) if b > a)
        merged: list[tuple[float, float]] = []
        for a, b in spans:
            if merged and a <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        return sum(max(0.0, min(end, b) - max(start, a)) for a, b in merged)

    def estimate(
        self,
        now: float,
        remaining: int,
        concurrency: int = 1,
        sample_durations: Iterable[float] = (),
        repair_pass_leads: int = 0,
    ) -> Eta:
        if remaining < 0 or concurrency < 1:
            raise ValueError("remaining must be non-negative and concurrency positive")
        points = sorted(ts for ts in self._completed if ts <= now)
        if len(points) > 10:
            points = points[-max(10, len(points)) :]
        intervals = [b - a - self._excluded(a, b) for a, b in pairwise(points)]
        seeded = len(points) < 10
        if seeded:
            durations = [value for value in sample_durations if value > 0]
            if not durations:
                return Eta(math.inf, math.inf, True)
            low = _percentile(durations, 25) / concurrency * (remaining + repair_pass_leads)
            high = _percentile(durations, 75) / concurrency * (remaining + repair_pass_leads)
        else:
            active = points[-1] - points[0] - self._excluded(points[0], points[-1])
            if active <= 0 or not intervals:
                return Eta(math.inf, math.inf, False)
            rate = len(intervals) / active
            spread = 1.96 / math.sqrt(len(intervals))
            low_rate = rate / (1 + spread)
            high_rate = rate / max(0.01, 1 - spread)
            low = (remaining + repair_pass_leads) / high_rate
            high = (remaining + repair_pass_leads) / low_rate
        return Eta(low, max(low, high), seeded)

    def with_quota(
        self,
        eta: Eta,
        snapshot_history: Iterable[QuotaPoint],
        remaining: int,
        stop_percent: int,
        now: float | None = None,
    ) -> Eta:
        points = sorted(snapshot_history, key=lambda point: point.taken_at)
        if len(points) < 2 or remaining <= 0:
            return eta
        decrease = max(
            (index for index, (a, b) in enumerate(pairwise(points)) if b.used_percent < a.used_percent),
            default=-1,
        )
        first_index = decrease + 1
        first, last = points[first_index], points[-1]
        if last.used_percent <= first.used_percent:
            return eta
        completions = sum(first.taken_at <= ts <= last.taken_at for ts in self._completed)
        if completions == 0:
            return eta
        percent_per_lead = (last.used_percent - first.used_percent) / completions
        if percent_per_lead <= 0:
            return eta
        capacity = max(0.0, (stop_percent - last.used_percent) / percent_per_lead)
        fraction = min(1.0, capacity / remaining)
        current = last.taken_at if now is None else now
        if fraction >= 1.0:
            return eta
        if last.resets_at is None:
            return Eta(eta.low, math.inf, eta.seeded, fraction, None)
        reset = float(last.resets_at)
        if current + fraction * eta.high >= reset:
            return eta
        wait = max(0.0, reset - current)
        return Eta(
            wait + (1 - fraction) * eta.low, wait + (1 - fraction) * eta.high, eta.seeded, fraction, reset
        )


def _percentile(values: list[float], percentile: int) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    position = (len(values) - 1) * percentile / 100
    lower, upper = math.floor(position), math.ceil(position)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)
