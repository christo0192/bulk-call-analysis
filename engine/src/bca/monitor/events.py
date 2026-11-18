from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Literal, TypeVar

EventKind = Literal[
    "state",
    "lead_done",
    "lead_failed",
    "activity",
    "quota",
    "pause",
    "resume",
    "stall",
    "slow",
    "sleep_detected",
    "wake",
    "eta",
    "counts",
    "notice",
]


@dataclass(frozen=True)
class Event:
    kind: EventKind
    job_id: str | None
    ts: float
    data: dict[str, object]


T = TypeVar("T")


class Subscription[T]:
    def __init__(self, limit: int) -> None:
        if limit < 1:
            raise ValueError("subscription limit must be positive")
        self._events: deque[T] = deque(maxlen=limit)
        self.dropped = 0
        self.closed = False

    def put(self, item: T) -> None:
        if self.closed:
            return
        if len(self._events) == self._events.maxlen:
            self._events.popleft()
            self.dropped += 1
        self._events.append(item)

    def get_nowait(self) -> T:
        if not self._events:
            raise LookupError("subscription is empty")
        return self._events.popleft()

    def drain(self) -> list[T]:
        items = list(self._events)
        self._events.clear()
        return items

    def close(self) -> None:
        self.closed = True


class EventBus:
    def __init__(self, queue_size: int = 100) -> None:
        if queue_size < 1:
            raise ValueError("queue_size must be positive")
        self.queue_size = queue_size
        self._subscribers: list[Subscription[Event]] = []

    def subscribe(self, queue_size: int | None = None) -> Subscription[Event]:
        subscription: Subscription[Event] = Subscription(queue_size or self.queue_size)
        self._subscribers.append(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription[Event]) -> None:
        subscription.close()
        if subscription in self._subscribers:
            self._subscribers.remove(subscription)

    def publish(self, event: Event) -> None:
        for subscriber in tuple(self._subscribers):
            subscriber.put(event)
