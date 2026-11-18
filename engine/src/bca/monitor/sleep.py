from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from .events import Event


@dataclass(frozen=True)
class WakeAction:
    name: str


class Clock(Protocol):
    def monotonic(self) -> float: ...


class SleepDetector:
    def __init__(
        self,
        heartbeat_s: float = 5,
        gap_factor: float = 3,
        network_probe: Callable[[], object] | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.heartbeat_s = heartbeat_s
        self.gap_factor = gap_factor
        self.network_probe = network_probe
        self._last: float | None = None
        self._sleeping = False
        self._wake_at: float | None = None
        self._suspend_at: float | None = None
        self._clock = clock
        self._last_mono: float | None = None

    def heartbeat(self, ts: float, job_id: str | None = None) -> list[Event]:
        events: list[Event] = []
        mono = self._clock.monotonic() if self._clock is not None else ts
        wall_gap = 0.0 if self._last is None else ts - self._last
        mono_gap = 0.0 if self._last_mono is None else mono - self._last_mono
        unexpected = wall_gap - self.heartbeat_s if self._clock is None else wall_gap - mono_gap
        threshold = 15.0
        if self._last is not None and unexpected > threshold:
            self._sleeping = True
            start = self._suspend_at if self._suspend_at is not None else self._last
            events.append(Event("sleep_detected", job_id, ts, {"start": start, "end": ts}))
            events.append(Event("wake", job_id, ts, {}))
            self._wake_at = ts
            self._sleeping = False
            self._suspend_at = None
        self._last = ts
        self._last_mono = mono
        return events

    def os_suspend_hint(self, ts: float | None = None) -> None:
        self._sleeping = True
        if ts is not None:
            self._suspend_at = ts

    def os_resume_hint(self, ts: float) -> list[Event]:
        if self._wake_at is not None and 0 <= ts - self._wake_at <= 120:
            return []
        start = self._suspend_at if self._suspend_at is not None else self._last
        self._sleeping = False
        self._wake_at = ts
        self._last = ts
        self._suspend_at = None
        events: list[Event] = []
        if start is not None and start < ts:
            events.append(Event("sleep_detected", None, ts, {"start": start, "end": ts}))
        events.append(Event("wake", None, ts, {"hint": True}))
        return events

    def wait_for_network(self, attempts: int = 4) -> bool:
        if self.network_probe is None:
            return True
        return any(self.network_probe() for _ in range(attempts))

    def wake_actions(self) -> tuple[WakeAction, ...]:
        return tuple(
            WakeAction(name) for name in ("requeue_in_flight", "reread_quota", "network_probe", "resume")
        )

    def post_wake_error(self, ts: float) -> bool:
        return self._wake_at is not None and 0 <= ts - self._wake_at <= 120

    @property
    def sleeping(self) -> bool:
        return self._sleeping
