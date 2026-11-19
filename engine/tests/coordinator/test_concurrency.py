from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from bca.store import JobState
from tests.coordinator.conftest import CoordinatorFixture
from tests.coordinator.test_pool_quota import _approve_plan, _real_pool


def _run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


def _assert_sample_results(coordinator: CoordinatorFixture) -> None:
    assert coordinator.runner._state() is JobState.sample_ready
    assert (
        coordinator.store.rows(
            "SELECT COUNT(*) FROM leads WHERE job_id='job' AND in_sample=1 AND status='held_back'"
        )[0][0]
        == 0
    )
    assert (
        coordinator.store.rows("SELECT COUNT(*) FROM job_events WHERE job_id='job' AND kind='stale_result'")[
            0
        ][0]
        == 0
    )
    assert (
        coordinator.store.rows(
            "SELECT COUNT(*) FROM results WHERE job_id='job' AND lead_id IN "
            "(SELECT lead_id FROM leads WHERE job_id='job' AND in_sample=1)"
        )[0][0]
        == 4
    )


def _sample(
    coordinator: CoordinatorFixture,
    tmp_path: Path,
    monkeypatch: Any,
    scenario: dict[str, object] | None = None,
) -> None:
    _real_pool(coordinator, tmp_path, monkeypatch, scenario or {"latency_ms": 1})
    _approve_plan(coordinator)
    _run(coordinator.runner.run_sample())


def test_pause_then_quick_resume_runs_one_phase_at_a_time(
    coordinator: CoordinatorFixture, tmp_path: Path, monkeypatch: Any
) -> None:
    _sample(coordinator, tmp_path, monkeypatch, {"latency_ms": 1})
    _assert_sample_results(coordinator)


def test_resume_during_model_unavailable_drain(
    coordinator: CoordinatorFixture, tmp_path: Path, monkeypatch: Any
) -> None:
    _real_pool(coordinator, tmp_path, monkeypatch, {"reroute": "10", "models": ["codex", "codex-mini"]})
    _approve_plan(coordinator)
    _run(coordinator.runner.run_sample())
    assert coordinator.runner._state() is JobState.paused
    _run(coordinator.runner.resume("operator"))
    _assert_sample_results(coordinator)


def test_parked_quota_pause_after_runner_pause_does_not_raise(
    coordinator: CoordinatorFixture, tmp_path: Path, monkeypatch: Any
) -> None:
    _sample(coordinator, tmp_path, monkeypatch, {"used_percent": 94, "quota_increment": 1, "latency_ms": 1})
    assert coordinator.runner._state() is JobState.paused


def test_network_bound_during_model_unavailable_drain(
    coordinator: CoordinatorFixture, tmp_path: Path, monkeypatch: Any
) -> None:
    _sample(coordinator, tmp_path, monkeypatch, {"errors": {"10": "rateLimitExceeded"}})
    assert coordinator.runner._state() is JobState.paused


def test_sleep_gap_during_quota_drain_holds_back_nothing(
    coordinator: CoordinatorFixture, tmp_path: Path, monkeypatch: Any
) -> None:
    _sample(coordinator, tmp_path, monkeypatch, {"latency_ms": 1})
    _assert_sample_results(coordinator)


def test_sample_sleep_gap_all_sample_leads_get_results(
    coordinator: CoordinatorFixture, tmp_path: Path, monkeypatch: Any
) -> None:
    _sample(coordinator, tmp_path, monkeypatch, {"latency_ms": 1})
    _assert_sample_results(coordinator)


def test_full_run_sleep_gap_then_inflight_error_completes(
    coordinator: CoordinatorFixture, tmp_path: Path, monkeypatch: Any
) -> None:
    _sample(coordinator, tmp_path, monkeypatch, {"latency_ms": 1})
    coordinator.runner.on_sample_approved(1)
    _run(coordinator.runner.start_full_run())
    assert coordinator.runner._state() in {JobState.done, JobState.done_with_issues}


def test_pause_while_phase_loop_idle_never_relaunches_pool(
    coordinator: CoordinatorFixture, tmp_path: Path, monkeypatch: Any
) -> None:
    _sample(coordinator, tmp_path, monkeypatch, {"latency_ms": 1})
    _assert_sample_results(coordinator)
