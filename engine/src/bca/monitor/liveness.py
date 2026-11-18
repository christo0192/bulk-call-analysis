from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .events import Event


@dataclass
class _Lead:
    started: float
    warned: bool = False


class Liveness:
    def __init__(
        self, concurrency_fn: Callable[[], int], slow_p95_s: float | None, pool_silence_s: float = 180
    ) -> None:
        self.concurrency_fn = concurrency_fn
        self.slow_p95_s = slow_p95_s
        self.pool_silence_s = pool_silence_s
        self.last_activity: float | None = None
        self._leads: dict[str, _Lead] = {}
        self._sleeping = False
        self._sleep_started: float | None = None

    def on_activity(self, ts: float) -> None:
        self.last_activity = ts

    def lead_started(self, lead: str, ts: float) -> None:
        self._leads[lead] = _Lead(ts)
        self.on_activity(ts)

    def lead_ended(self, lead: str, ts: float) -> None:
        self._leads.pop(lead, None)
        self.on_activity(ts)

    def sleep_detected(self, ts: float | None = None) -> None:
        self._sleeping = True
        if self._sleep_started is None:
            self._sleep_started = ts if ts is not None else self.last_activity

    def wake(self, ts: float) -> None:
        if self._sleep_started is not None:
            slept_for = max(0.0, ts - self._sleep_started)
            for lead in self._leads.values():
                lead.started += slept_for
            if self.last_activity is not None:
                self.last_activity += slept_for
            self._sleep_started = None
        self._sleeping = False
        self.on_activity(ts)

    def check(self, now: float) -> list[Event]:
        events: list[Event] = []
        if self.slow_p95_s is not None:
            for lead, state in self._leads.items():
                if not state.warned and now - state.started > self.slow_p95_s:
                    state.warned = True
                    events.append(Event("slow", None, now, {"lead": lead, "elapsed": now - state.started}))
        if (
            not self._sleeping
            and self._leads
            and self.concurrency_fn() >= 3
            and self.last_activity is not None
            and now - self.last_activity >= self.pool_silence_s
        ):
            events.append(Event("stall", None, now, {"in_flight": len(self._leads)}))
            self.last_activity = now
        return events
