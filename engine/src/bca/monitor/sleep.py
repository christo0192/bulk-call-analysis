from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .events import Event


@dataclass(frozen=True)
class WakeAction:
    name: str


class SleepDetector:
    def __init__(
        self, heartbeat_s: float = 5, gap_factor: float = 3, network_probe: Callable[[], object] | None = None
    ) -> None:
        self.heartbeat_s = heartbeat_s
        self.gap_factor = gap_factor
        self.network_probe = network_probe
        self._last: float | None = None
        self._sleeping = False
        self._wake_at: float | None = None

    def heartbeat(self, ts: float, job_id: str | None = None) -> list[Event]:
        events: list[Event] = []
        if self._last is not None and ts - self._last > self.gap_factor * self.heartbeat_s:
            self._sleeping = True
            events.append(Event("sleep_detected", job_id, ts, {"start": self._last, "end": ts}))
            events.append(Event("wake", job_id, ts, {}))
            self._wake_at = ts
        self._last = ts
        return events

    def os_suspend_hint(self, ts: float | None = None) -> None:
        self._sleeping = True
        if ts is not None:
            self._last = ts

    def os_resume_hint(self, ts: float) -> list[Event]:
        self._sleeping = False
        self._wake_at = ts
        self._last = ts
        return [Event("wake", None, ts, {"hint": True})]

    def wake_actions(self) -> tuple[WakeAction, ...]:
        return tuple(
            WakeAction(name) for name in ("requeue_in_flight", "reread_quota", "network_probe", "resume")
        )

    def post_wake_error(self, ts: float) -> bool:
        return self._wake_at is not None and 0 <= ts - self._wake_at <= 120

    @property
    def sleeping(self) -> bool:
        return self._sleeping
