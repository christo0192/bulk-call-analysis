import threading

from bca.monitor.liveness import Liveness
from bca.monitor.sleep import SleepDetector


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def now(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def test_slow_and_pool_stall_rules() -> None:
    live = Liveness(lambda: 4, slow_p95_s=5, pool_silence_s=10)
    for lead in ("a", "b", "c", "d"):
        live.lead_started(lead, 0)
    kinds = [event.kind for event in live.check(6)]
    assert kinds.count("slow") == 4
    assert "stall" not in kinds
    assert "stall" in [event.kind for event in live.check(11)]


def test_slow_lead_is_reported_past_sample_p95() -> None:
    clock = FakeClock()
    live = Liveness(lambda: 2, slow_p95_s=5, pool_silence_s=30)
    live.lead_started("slow", clock.now())
    clock.advance(6)
    events = live.check(clock.now())
    assert [(event.kind, event.data["lead"]) for event in events] == [("slow", "slow")]


def test_pool_stall_requires_concurrency_four_and_silence_threshold() -> None:
    clock = FakeClock()
    live = Liveness(lambda: 4, slow_p95_s=None, pool_silence_s=10)
    live.lead_started("a", clock.now())
    clock.advance(9)
    assert not live.check(clock.now())
    clock.advance(1)
    assert not live.check(clock.now())


def test_concurrency_two_reports_only_slow_not_stall() -> None:
    live = Liveness(lambda: 2, slow_p95_s=5, pool_silence_s=10)
    live.lead_started("a", 0)
    assert [event.kind for event in live.check(20)] == ["slow"]


def test_liveness_excludes_detected_sleep_gap() -> None:
    live = Liveness(lambda: 4, slow_p95_s=5, pool_silence_s=10)
    live.lead_started("a", 0)
    live.sleep_detected(1)
    live.wake(101)
    assert not live.check(105)
    assert [event.kind for event in live.check(112)] == ["slow"]


def test_no_stall_at_low_concurrency_or_across_sleep() -> None:
    live = Liveness(lambda: 2, slow_p95_s=None, pool_silence_s=10)
    live.lead_started("a", 0)
    assert not live.check(20)
    live2 = Liveness(lambda: 4, slow_p95_s=None, pool_silence_s=10)
    live2.lead_started("a", 0)
    live2.sleep_detected()
    assert not live2.check(20)


def test_liveness_check_is_safe_during_lead_changes() -> None:
    live = Liveness(lambda: 4, slow_p95_s=5, pool_silence_s=10)
    errors: list[BaseException] = []

    def mutate() -> None:
        try:
            for index in range(200):
                live.lead_started(str(index), float(index))
                live.lead_ended(str(index), float(index) + 0.5)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=mutate)
    thread.start()
    for index in range(200):
        live.check(float(index))
    thread.join()
    assert errors == []


def test_sleep_gap_wake_order_and_grace() -> None:
    detector = SleepDetector(heartbeat_s=5, gap_factor=3)
    assert detector.heartbeat(0) == []
    assert detector.heartbeat(16) == []
    events = detector.heartbeat(37)
    assert [event.kind for event in events] == ["sleep_detected", "wake"]
    assert [action.name for action in detector.wake_actions()] == [
        "requeue_in_flight",
        "reread_quota",
        "network_probe",
        "resume",
    ]
    assert detector.post_wake_error(100)
    assert not detector.post_wake_error(158)


def test_sleep_gap_detection_uses_fake_clock() -> None:
    clock = FakeClock()
    detector = SleepDetector(heartbeat_s=5, gap_factor=3)
    assert detector.heartbeat(clock.now()) == []
    clock.advance(21)
    events = detector.heartbeat(clock.now(), "job")
    assert events[0].data == {"start": 0.0, "end": 21.0}
    assert not detector.sleeping


def test_os_suspend_and_resume_hints() -> None:
    detector = SleepDetector()
    detector.heartbeat(10)
    detector.os_suspend_hint(20)
    assert detector.sleeping
    events = detector.os_resume_hint(50)
    assert not detector.sleeping
    assert events[0].kind == "sleep_detected"
    assert events[0].data == {"start": 20, "end": 50}


def test_post_wake_connectivity_error_grace_is_120_seconds() -> None:
    detector = SleepDetector()
    detector.os_resume_hint(100)
    assert detector.post_wake_error(220)
    assert not detector.post_wake_error(221)


def test_resume_hint_reports_interval_once_and_probes_network() -> None:
    calls: list[int] = []

    def probe() -> bool:
        calls.append(1)
        return True

    detector = SleepDetector(network_probe=probe)
    detector.heartbeat(10)
    detector.os_suspend_hint(12)
    events = detector.os_resume_hint(3612)
    assert [event.kind for event in events] == ["sleep_detected", "wake"]
    assert events[0].data == {"start": 12, "end": 3612}
    assert detector.os_resume_hint(3613) == []
    assert detector.wait_for_network() and calls
