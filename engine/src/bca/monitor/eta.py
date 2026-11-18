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
        return sum(max(0.0, min(end, b) - max(start, a)) for a, b in (*self._paused, *self._sleep) if b > a)

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
        points = [ts for ts in self._completed if now - self.window_s <= ts <= now]
        intervals = [b - a - self._excluded(a, b) for a, b in pairwise(points)]
        intervals = [value for value in intervals if value > 0]
        seeded = len(points) < 10
        if seeded:
            durations = [value for value in sample_durations if value > 0]
            base = (sum(durations) / len(durations) / concurrency) if durations else 0.0
            low = high = base * (remaining + repair_pass_leads)
        else:
            low_rate = _percentile(intervals, 25) / max(1, concurrency) if intervals else 0.0
            high_rate = _percentile(intervals, 75) / max(1, concurrency) if intervals else 0.0
            low = low_rate * (remaining + repair_pass_leads)
            high = high_rate * (remaining + repair_pass_leads)
        return Eta(low, max(low, high), seeded)

    def with_quota(
        self, eta: Eta, snapshot_history: Iterable[QuotaPoint], remaining: int, stop_percent: int
    ) -> Eta:
        points = sorted(snapshot_history, key=lambda point: point.taken_at)
        movement = [(a, b) for a, b in pairwise(points) if b.used_percent > a.used_percent]
        if not movement or remaining <= 0:
            return eta
        first, last = movement[0][0], movement[-1][1]
        percent_per_lead = (last.used_percent - first.used_percent) / max(1, len(points) - 1)
        if percent_per_lead <= 0:
            return eta
        capacity = max(0.0, (stop_percent - last.used_percent) / percent_per_lead)
        fraction = min(1.0, capacity / remaining)
        if fraction >= 1.0 or last.resets_at is None:
            return eta
        reset = float(last.resets_at)
        wait = max(0.0, reset - last.taken_at)
        extra = eta.high * (remaining - capacity) / max(1.0, remaining)
        return Eta(eta.low + wait * (1 - fraction), eta.high + wait + extra, eta.seeded, fraction, reset)


def _percentile(values: list[float], percentile: int) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    position = (len(values) - 1) * percentile / 100
    lower, upper = math.floor(position), math.ceil(position)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)
