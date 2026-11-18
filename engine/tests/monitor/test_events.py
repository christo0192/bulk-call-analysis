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
