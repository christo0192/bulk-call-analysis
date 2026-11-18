import threading

from bca.monitor.events import Event, EventBus


def test_drop_oldest_and_multiple_subscribers() -> None:
    bus = EventBus(queue_size=2)
    first = bus.subscribe()
    second = bus.subscribe()
    for ts in (1.0, 2.0, 3.0):
        bus.publish(Event("activity", "job", ts, {}))
    assert [event.ts for event in first.drain()] == [2.0, 3.0]
    assert [event.ts for event in second.drain()] == [2.0, 3.0]
    assert first.dropped == second.dropped == 1


def test_slow_subscriber_does_not_block_producer() -> None:
    bus = EventBus(queue_size=1)
    slow = bus.subscribe()
    bus.publish(Event("activity", "job", 1.0, {}))
    bus.publish(Event("activity", "job", 2.0, {}))
    assert slow.dropped == 1
    assert slow.get_nowait().ts == 2.0


def test_each_subscriber_receives_every_event_when_queues_have_capacity() -> None:
    bus = EventBus(queue_size=3)
    subscribers = [bus.subscribe() for _ in range(2)]
    events = [Event("activity", "job", float(ts), {}) for ts in (1, 2, 3)]
    for event in events:
        bus.publish(event)
    assert [sub.drain() for sub in subscribers] == [events, events]


def test_concurrent_publish_accounts_for_every_eviction() -> None:
    bus = EventBus(queue_size=8)
    subscription = bus.subscribe()
    total = 4 * 1000

    def publish(offset: int) -> None:
        for index in range(1000):
            bus.publish(Event("activity", None, float(offset + index), {}))

    threads = [threading.Thread(target=publish, args=(offset * 1000,)) for offset in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(subscription.drain()) + subscription.dropped == total
