from bca.monitor.liveness import Liveness
from bca.monitor.sleep import SleepDetector


def test_slow_and_pool_stall_rules() -> None:
    live = Liveness(lambda: 4, slow_p95_s=5, pool_silence_s=10)
    for lead in ("a", "b", "c", "d"):
        live.lead_started(lead, 0)
    kinds = [event.kind for event in live.check(6)]
    assert kinds.count("slow") == 4
    assert "stall" not in kinds
    assert "stall" in [event.kind for event in live.check(11)]


def test_no_stall_at_low_concurrency_or_across_sleep() -> None:
    live = Liveness(lambda: 2, slow_p95_s=None, pool_silence_s=10)
    live.lead_started("a", 0)
    assert not live.check(20)
    live2 = Liveness(lambda: 4, slow_p95_s=None, pool_silence_s=10)
    live2.lead_started("a", 0)
    live2.sleep_detected()
    assert not live2.check(20)


def test_sleep_gap_wake_order_and_grace() -> None:
    detector = SleepDetector(heartbeat_s=5, gap_factor=3)
    assert detector.heartbeat(0) == []
    events = detector.heartbeat(16)
    assert [event.kind for event in events] == ["sleep_detected", "wake"]
    assert [action.name for action in detector.wake_actions()] == [
        "requeue_in_flight",
        "reread_quota",
        "network_probe",
        "resume",
    ]
    assert detector.post_wake_error(100)
    assert not detector.post_wake_error(137)
