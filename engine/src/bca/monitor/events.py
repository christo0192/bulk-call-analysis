from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, TypeVar

EventKind = Literal[
    "state",
    "lead_done",
    "lead_failed",
    "lead_started",
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
        self._lock = threading.Lock()
        self._wake: Callable[[], None] | None = None
        self.dropped = 0
        self.closed = False

    def put(self, item: T) -> None:
        with self._lock:
            if self.closed:
                return
            if len(self._events) == self._events.maxlen:
                self._events.popleft()
                self.dropped += 1
            self._events.append(item)
            wake = self._wake
        if wake is not None:
            wake()

    def set_wake(self, wake: Callable[[], None] | None) -> None:
        with self._lock:
            self._wake = wake

    def get_nowait(self) -> T:
        with self._lock:
            if not self._events:
                raise LookupError("subscription is empty")
            return self._events.popleft()

    def drain(self) -> list[T]:
        with self._lock:
            items = list(self._events)
            self._events.clear()
            return items

    def close(self) -> None:
        with self._lock:
            self.closed = True


class EventBus:
    def __init__(self, queue_size: int = 100) -> None:
        if queue_size < 1:
            raise ValueError("queue_size must be positive")
        self.queue_size = queue_size
        self._subscribers: list[Subscription[Event]] = []
        self._lock = threading.Lock()

    def subscribe(self, queue_size: int | None = None) -> Subscription[Event]:
        subscription: Subscription[Event] = Subscription(queue_size or self.queue_size)
        with self._lock:
            self._subscribers.append(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription[Event]) -> None:
        subscription.close()
        with self._lock:
            if subscription in self._subscribers:
                self._subscribers.remove(subscription)

    def publish(self, event: Event) -> None:
        with self._lock:
            subscribers = tuple(self._subscribers)
        for subscriber in subscribers:
            subscriber.put(event)
