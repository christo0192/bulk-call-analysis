from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from bca.outputs.verify import verify_job
from bca.store import (
    JobState,
    PauseReason,
    TransitionError,
    approve_plan,
    approve_sample,
    reconcile,
    start_full_run,
)
from bca.store.lifecycle import pause
from tests.coordinator.conftest import CoordinatorFixture


def _run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


def _statuses(fixture: CoordinatorFixture) -> dict[str, str]:
    return {
        str(row[0]): str(row[1])
        for row in fixture.store.rows("SELECT lead_id, status FROM leads WHERE job_id=?", ("job",))
    }


def test_full_job_happy_path(coordinator: CoordinatorFixture) -> None:
    coordinator.runner.on_plan_approved(1)
    _run(coordinator.runner.run_sample())
    coordinator.runner.on_sample_approved(1)
    _run(coordinator.runner.start_full_run())

    assert coordinator.runner._state() is JobState.done
    workbook = coordinator.job_dir / "output" / "results.v1.xlsx"
    assert workbook.is_file()
    report = verify_job(coordinator.store, "job", workbook)
    assert report.ok
    reconciliation = reconcile(coordinator.store, "job", final=True)
    assert reconciliation.ok
    statuses = _statuses(coordinator)
    assert len(statuses) == 40
    assert sum(status == "done" for status in statuses.values()) == 34
    assert coordinator.store.rows("SELECT lead_id FROM results WHERE job_id=?", ("job",))


def test_edge_leads_get_correct_outcomes_and_reconcile(coordinator: CoordinatorFixture) -> None:
    coordinator.runner.on_plan_approved(1)
    _run(coordinator.runner.run_sample())
    coordinator.runner.on_sample_approved(1)
    _run(coordinator.runner.start_full_run())

    statuses = _statuses(coordinator)
    assert {statuses[str(index)] for index in range(3)} == {"no_transcript"}
    assert {statuses[str(index)] for index in range(3, 6)} == {"too_short"}
    assert statuses["6"] == "done"
    assert int(coordinator.store.rows("SELECT calls_dropped_cap FROM leads WHERE lead_id='6'")[0][0]) == 1
    assert reconcile(coordinator.store, "job", final=True).ok


@pytest.mark.parametrize("gate", ["plan", "sample", "full"])
def test_automated_actor_cannot_pass_any_gate(coordinator: CoordinatorFixture, gate: str) -> None:
    if gate == "plan":
        with pytest.raises(TransitionError):
            approve_plan(coordinator.store, "job", cast(Any, "automation"), 1)
    elif gate == "sample":
        coordinator.store.rows("UPDATE jobs SET state='sample_ready' WHERE id='job'")
        with pytest.raises(TransitionError):
            approve_sample(coordinator.store, "job", cast(Any, "automation"), 1)
    else:
        coordinator.store.rows("UPDATE jobs SET state='sample_approved' WHERE id='job'")
        with pytest.raises(TransitionError):
            start_full_run(coordinator.store, "job", cast(Any, "automation"), 1)


@pytest.mark.parametrize("gate", ["plan", "sample", "full"])
def test_stale_spec_version_is_refused(coordinator: CoordinatorFixture, gate: str) -> None:
    if gate == "plan":
        with pytest.raises(TransitionError):
            approve_plan(coordinator.store, "job", "operator", 99)
    elif gate == "sample":
        coordinator.store.rows("UPDATE jobs SET state='sample_ready' WHERE id='job'")
        with pytest.raises(TransitionError):
            approve_sample(coordinator.store, "job", "operator", 99)
    else:
        coordinator.store.rows("UPDATE jobs SET state='sample_approved' WHERE id='job'")
        with pytest.raises(TransitionError):
            start_full_run(coordinator.store, "job", "operator", 99)


def test_quota_stop_then_canary_then_full_concurrency(coordinator: CoordinatorFixture) -> None:
    coordinator.pool.used_percent = 94
    coordinator.pool.quota_increment = 1
    coordinator.runner.on_plan_approved(1)
    _run(coordinator.runner.run_sample())
    assert coordinator.runner._state() is JobState.paused
    first_done = len(coordinator.store.rows("SELECT lead_id FROM results WHERE job_id=?", ("job",)))
    assert first_done == 1
    coordinator.pool.used_percent = 0
    coordinator.pool.quota_increment = 0
    _run(coordinator.runner.resume())
    assert coordinator.runner._state() is JobState.sample_ready
    assert len(coordinator.store.rows("SELECT lead_id FROM results WHERE job_id=?", ("job",))) == 4


def test_quota_resume_keeps_event_loop_live(
    coordinator: CoordinatorFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator.pool.used_percent = 94
    coordinator.pool.quota_increment = 1
    coordinator.runner.on_plan_approved(1)
    _run(coordinator.runner.run_sample())
    assert coordinator.runner._state() is JobState.paused
    pool = coordinator.runner._phase_pool
    assert pool is not None
    run_until_idle = pool.run_until_idle

    async def delayed_run_until_idle() -> None:
        await run_until_idle()
        await asyncio.sleep(0.25)

    monkeypatch.setattr(pool, "run_until_idle", delayed_run_until_idle)

    async def resume_with_ticker() -> int:
        ticks = 0
        running = True

        async def ticker() -> None:
            nonlocal ticks
            while running:
                await asyncio.sleep(0.01)
                ticks += 1

        task = asyncio.create_task(ticker())
        try:
            coordinator.pool.used_percent = 0
            coordinator.pool.quota_increment = 0
            await asyncio.wait_for(coordinator.runner.resume(), timeout=10)
        finally:
            running = False
            await task
        return ticks

    assert _run(resume_with_ticker()) >= 20


def test_credit_signal_interrupts_and_pauses_without_further_dispatch(
    coordinator: CoordinatorFixture,
) -> None:
    coordinator.pool.actions["10"] = "credit_signal"
    coordinator.runner.on_plan_approved(1)
    _run(coordinator.runner.run_sample())
    assert coordinator.runner._state() is JobState.paused
    assert coordinator.store.rows("SELECT COUNT(*) FROM attempts WHERE job_id=?", ("job",))[0][0] == 1
    assert coordinator.store.rows("SELECT pause_reason FROM jobs WHERE id='job'")[0][0] == PauseReason.credit


@pytest.mark.parametrize(
    "reason", [PauseReason.user, PauseReason.auth, PauseReason.model_unavailable, PauseReason.error_storm]
)
def test_operator_only_resume_reasons(coordinator: CoordinatorFixture, reason: PauseReason) -> None:
    coordinator.store.rows("UPDATE jobs SET state='sample_running' WHERE id='job'")
    pause(coordinator.store, "job", reason)
    with pytest.raises(PermissionError):
        _run(coordinator.runner.resume("automatic"))
    assert coordinator.runner._state() is JobState.paused
