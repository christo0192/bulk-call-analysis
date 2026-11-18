from dataclasses import dataclass

from bca.monitor.eta import EtaEstimator


@dataclass
class Point:
    used_percent: int
    resets_at: int | None
    taken_at: float


class FakeClock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def now(self) -> float:
        return self.value

    def monotonic(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def test_seed_then_real_range_and_repair() -> None:
    estimator = EtaEstimator()
    for ts in (10.0, 20.0, 30.0):
        estimator.completed(ts)
    seeded = estimator.estimate(30, 4, sample_durations=(10, 20), repair_pass_leads=1)
    assert seeded.seeded and seeded.high == 75
    for ts in range(40, 140, 10):
        estimator.completed(float(ts))
    real = estimator.estimate(140, 2)
    assert not real.seeded and real.low <= real.high


def test_steady_throughput_gives_expected_remaining_time() -> None:
    clock = FakeClock()
    estimator = EtaEstimator()
    for _ in range(10):
        estimator.completed(clock.now())
        clock.advance(10)
    eta = estimator.estimate(clock.now() - 10, remaining=4, concurrency=2)
    assert eta.low == eta.high == 20


def test_seed_uses_sample_durations_until_ten_completions() -> None:
    estimator = EtaEstimator()
    for ts in range(9):
        estimator.completed(float(ts))
    seeded = estimator.estimate(8, remaining=2, concurrency=2, sample_durations=(8, 12))
    assert seeded.seeded and seeded.low == seeded.high == 10

    estimator.completed(9)
    real = estimator.estimate(9, remaining=2, concurrency=2)
    assert not real.seeded


def test_repair_pass_leads_are_included_in_eta() -> None:
    estimator = EtaEstimator()
    for ts in range(10):
        estimator.completed(float(ts * 10))
    eta = estimator.estimate(90, remaining=2, repair_pass_leads=3)
    assert eta.low <= eta.high
    assert eta.high == 50


def test_paused_and_sleep_intervals_are_excluded() -> None:
    estimator = EtaEstimator()
    estimator.pause(10, 30)
    estimator.sleep_gap(40, 80)
    for ts in range(0, 100, 10):
        estimator.completed(float(ts))
    estimator.completed(100)
    assert estimator.estimate(100, 1).high == 10


def test_range_ordering_is_preserved_for_real_window() -> None:
    estimator = EtaEstimator()
    for ts in range(0, 100, 10):
        estimator.completed(float(ts))
    eta = estimator.estimate(90, remaining=3, concurrency=1)
    assert eta.low <= eta.high


def test_quota_projection_requires_movement_and_includes_wait() -> None:
    estimator = EtaEstimator()
    eta = estimator.estimate(0, 2, sample_durations=(10,))
    unchanged = estimator.with_quota(eta, [Point(50, 100, 0)], 2, 80)
    projected = estimator.with_quota(eta, [Point(50, 100, 0), Point(70, 100, 10)], 2, 80)
    assert unchanged == eta
    assert projected.will_pause_at_fraction is not None
    assert projected.resume_at == 100
