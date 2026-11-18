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
    assert seeded.seeded and seeded.low == 62.5 and seeded.high == 87.5
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
    assert eta.low < eta.high


def test_real_eta_is_pool_rate_and_does_not_depend_on_concurrency() -> None:
    estimator = EtaEstimator()
    for ts in range(0, 100, 10):
        estimator.completed(float(ts))
    one_worker = estimator.estimate(90, remaining=4, concurrency=1)
    many_workers = estimator.estimate(90, remaining=4, concurrency=8)
    assert many_workers == one_worker


def test_unknown_real_window_is_not_reported_as_done_now() -> None:
    estimator = EtaEstimator()
    for _ in range(10):
        estimator.completed(5)
    eta = estimator.estimate(5, remaining=500)
    assert not eta.seeded and eta.low == eta.high == float("inf")


def test_slow_completions_leave_seeded_mode() -> None:
    estimator = EtaEstimator()
    for ts in range(0, 200 * 150, 150):
        estimator.completed(float(ts))
    eta = estimator.estimate(199 * 150, remaining=100)
    assert not eta.seeded and eta.high < float("inf")


def test_out_of_order_completions_are_sorted_before_rate_calculation() -> None:
    estimator = EtaEstimator()
    for ts in (0, 2, 1, 4, 3, 6, 5, 8, 7, 10):
        estimator.completed(float(ts))
    eta = estimator.estimate(10, remaining=10)
    assert not eta.seeded and eta.low > 0


def test_seed_uses_sample_durations_until_ten_completions() -> None:
    estimator = EtaEstimator()
    for ts in range(9):
        estimator.completed(float(ts))
    seeded = estimator.estimate(8, remaining=2, concurrency=2, sample_durations=(8, 12))
    assert seeded.seeded and seeded.low == 9 and seeded.high == 11

    estimator.completed(9)
    real = estimator.estimate(9, remaining=2, concurrency=2)
    assert not real.seeded


def test_repair_pass_leads_are_included_in_eta() -> None:
    estimator = EtaEstimator()
    for ts in range(10):
        estimator.completed(float(ts * 10))
    eta = estimator.estimate(90, remaining=2, repair_pass_leads=3)
    assert eta.low <= eta.high
    assert eta.high > eta.low


def test_paused_and_sleep_intervals_are_excluded() -> None:
    estimator = EtaEstimator()
    estimator.pause(10, 30)
    estimator.sleep_gap(40, 80)
    for ts in range(0, 100, 10):
        estimator.completed(float(ts))
    estimator.completed(100)
    eta = estimator.estimate(100, 1)
    assert eta.low <= eta.high and eta.high < 10


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
    estimator.completed(0)
    estimator.completed(10)
    projected = estimator.with_quota(eta, [Point(50, 100, 0), Point(70, 100, 10)], 2, 80, now=10)
    assert unchanged == eta
    assert projected.will_pause_at_fraction is not None
    assert projected.resume_at == 100 and projected.will_pause_at_fraction == 0.5
    assert projected.low == projected.high == 100


def test_quota_uses_completed_leads_and_latest_post_reset_snapshot() -> None:
    estimator = EtaEstimator()
    for ts in (10, 20, 30, 40, 50, 60):
        estimator.completed(ts)
    eta = EtaEstimator().estimate(0, 100, sample_durations=(10,))
    points = [Point(80, 150, 0), Point(90, 150, 60), Point(2, 18150, 120), Point(5, 18150, 180)]
    projected = estimator.with_quota(eta, points, 100, 10, now=180)
    assert projected.will_pause_at_fraction is None
    assert projected == eta


def test_quota_rate_is_per_completed_lead_and_skips_reset_before_limit() -> None:
    estimator = EtaEstimator()
    for ts in range(0, 100, 10):
        estimator.completed(float(ts))
    eta = EtaEstimator().estimate(0, 10, sample_durations=(10,))
    points = [Point(80, 1000, 0), Point(85, 1000, 50), Point(90, 1000, 100)]
    projected = estimator.with_quota(eta, points, 10, 95, now=100)
    assert projected.low == projected.high == 950

    before_reset = [Point(80, 120, 0), Point(85, 120, 50), Point(90, 120, 100)]
    assert estimator.with_quota(eta, before_reset, 10, 95, now=100) == eta


def test_quota_unknown_reset_marks_high_unknown() -> None:
    estimator = EtaEstimator()
    estimator.completed(10)
    estimator.completed(20)
    eta = EtaEstimator().estimate(0, 100, sample_durations=(10,))
    projected = estimator.with_quota(eta, [Point(80, None, 0), Point(90, None, 20)], 100, 95, now=20)
    assert projected.will_pause_at_fraction == 0.01
    assert projected.resume_at is None
    assert projected.high == float("inf")


def test_bursty_completions_use_pool_throughput_and_merge_exclusions() -> None:
    estimator = EtaEstimator()
    estimator.pause(10, 60)
    estimator.sleep_gap(30, 80)
    for ts in (0, 10, 20, 20, 20, 20, 60, 60, 60, 60):
        estimator.completed(ts)
    eta = estimator.estimate(60, 10)
    assert eta.low > 0 and eta.high < 100
