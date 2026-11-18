from dataclasses import dataclass

from bca.monitor.eta import EtaEstimator


@dataclass
class Point:
    used_percent: int
    resets_at: int | None
    taken_at: float


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


def test_paused_and_sleep_intervals_are_excluded() -> None:
    estimator = EtaEstimator()
    estimator.pause(10, 30)
    estimator.sleep_gap(40, 80)
    for ts in range(0, 100, 10):
        estimator.completed(float(ts))
    estimator.completed(100)
    assert estimator.estimate(100, 1).high == 10


def test_quota_projection_requires_movement_and_includes_wait() -> None:
    estimator = EtaEstimator()
    eta = estimator.estimate(0, 2, sample_durations=(10,))
    unchanged = estimator.with_quota(eta, [Point(50, 100, 0)], 2, 80)
    projected = estimator.with_quota(eta, [Point(50, 100, 0), Point(70, 100, 10)], 2, 80)
    assert unchanged == eta
    assert projected.will_pause_at_fraction is not None
    assert projected.resume_at == 100
