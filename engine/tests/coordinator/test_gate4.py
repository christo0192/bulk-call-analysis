from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import bca.coordinator.runner as runner_module
import bca.luna.pool as pool_module
from bca.luna.pool import LunaPool
from bca.store import JobState, TransitionError, edit_spec, save_spec
from tests.coordinator.conftest import CoordinatorFixture
from tests.coordinator.test_pool_quota import _real_pool


def _run(awaitable: Any, timeout: float = 20) -> Any:
    return asyncio.run(asyncio.wait_for(awaitable, timeout=timeout))


def _count(coordinator: CoordinatorFixture, sql: str, args: tuple[object, ...] = ()) -> int:
    return int(coordinator.store.rows(sql, args)[0][0])


def test_superseded_task_not_dispatched_after_edit_and_reapprove_during_drain(
    coordinator: CoordinatorFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario_path = _real_pool(
        coordinator,
        tmp_path,
        monkeypatch,
        {
            "reroute": ["11"],
            "models": ["codex", "codex-mini"],
            "errors": {"12": "responseStreamDisconnected"},
            "latency_ms": 400,
        },
    )
    dispatches: list[tuple[str, str, str]] = []

    class RecordingPool(LunaPool):
        async def _run_lead(self, lead: Any) -> None:
            dispatches.append((lead.task.lead_id, lead.task.attempt_id, lead.task.prompt))
            await super()._run_lead(lead)

    monkeypatch.setattr(runner_module, "LunaPool", RecordingPool)
    # results.attempt_id is the STORE attempt id, created in on_started; the pool task id differs.
    prompts_by_attempt: dict[str, str] = {}
    original_started = runner_module._Callbacks.on_started

    async def on_started(self: Any, task: Any) -> bool:
        started = bool(await original_started(self, task))
        store_attempt = self.runner._attempts.get(task.attempt_id)
        if store_attempt is not None:
            prompts_by_attempt[str(store_attempt)] = task.prompt
        return started

    monkeypatch.setattr(runner_module._Callbacks, "on_started", on_started)
    runner = coordinator.runner
    runner.on_plan_approved(1)

    async def scenario() -> None:
        old_sample = asyncio.create_task(runner.run_sample())
        deadline = time.monotonic() + 20
        while runner._state() is not JobState.paused and time.monotonic() < deadline:
            await asyncio.sleep(0.002)
        assert runner._state() is JobState.paused
        save_spec(replace(runner.spec, instructions="Return v2."), coordinator.job_dir)
        edit_spec(coordinator.store, "job", 2)
        with pytest.raises(TransitionError):
            runner.on_plan_approved(2)
        dispatch_count = len(dispatches)

        async def wait_for_drain() -> None:
            while runner._phase_running and not old_sample.done():
                await asyncio.sleep(0.002)

        await asyncio.wait_for(wait_for_drain(), timeout=20)
        assert len(dispatches) == dispatch_count
        runner.on_plan_approved(2)
        scenario_path.write_text(json.dumps({"latency_ms": 50}), encoding="utf-8", newline="\n")
        new_sample = asyncio.create_task(runner.run_sample())
        await asyncio.wait_for(old_sample, timeout=20)
        await asyncio.wait_for(new_sample, timeout=20)

    _run(scenario())
    approval_index = next(i for i, item in enumerate(dispatches) if "Return v2." in item[2])
    assert all("Return v2." in prompt for _, _, prompt in dispatches[approval_index:])
    stored = coordinator.store.rows(
        "SELECT r.spec_version, a.attempt_id "
        "FROM results r JOIN attempts a ON a.attempt_id=r.attempt_id WHERE r.job_id='job'"
    )
    assert stored
    assert all(row[0] == 2 and "Return v2." in prompts_by_attempt[str(row[1])] for row in stored)


def _jump_clock_on_first_result(coordinator: CoordinatorFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    original = runner_module._Callbacks.on_result
    seen = False

    async def on_result(self: Any, task: Any, answer: Any, model_used: Any, usage: Any) -> None:
        nonlocal seen
        await original(self, task, answer, model_used, usage)
        if not seen:
            seen = True
            coordinator.clock.current += 100

    monkeypatch.setattr(runner_module._Callbacks, "on_result", on_result)


def test_user_pause_while_paused_sleep_wins(
    coordinator: CoordinatorFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _real_pool(coordinator, tmp_path, monkeypatch, {"latency_ms": 300})
    _jump_clock_on_first_result(coordinator, monkeypatch)
    runner = coordinator.runner
    runner.on_plan_approved(1)

    async def scenario() -> None:
        sample = asyncio.create_task(runner.run_sample())
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            state, reason = coordinator.store.rows("SELECT state, pause_reason FROM jobs WHERE id='job'")[0]
            if state == "paused" and reason == "sleep":
                break
            await asyncio.sleep(0.002)
        assert coordinator.store.rows("SELECT pause_reason FROM jobs WHERE id='job'")[0][0] == "sleep"
        await runner.pause("user")
        await asyncio.wait_for(sample, timeout=20)

    _run(scenario())
    assert runner._state() is JobState.paused
    assert coordinator.store.rows("SELECT pause_reason FROM jobs WHERE id='job'")[0][0] == "user"


def test_wake_flag_cleared_by_plan_edit(
    coordinator: CoordinatorFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _real_pool(coordinator, tmp_path, monkeypatch, {"latency_ms": 300})
    _jump_clock_on_first_result(coordinator, monkeypatch)
    runner = coordinator.runner
    runner.on_plan_approved(1)

    async def first_phase() -> None:
        sample = asyncio.create_task(runner.run_sample())
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            state, reason = coordinator.store.rows("SELECT state, pause_reason FROM jobs WHERE id='job'")[0]
            if state == "paused" and reason == "sleep":
                break
            await asyncio.sleep(0.002)
        assert coordinator.store.rows("SELECT pause_reason FROM jobs WHERE id='job'")[0][0] == "sleep"
        save_spec(replace(runner.spec, instructions="Return v2."), coordinator.job_dir)
        edit_spec(coordinator.store, "job", 2)
        await asyncio.wait_for(sample, timeout=20)

    _run(first_phase())
    runner.on_plan_approved(2)

    async def second_phase() -> None:
        sample = asyncio.create_task(runner.run_sample())
        deadline = time.monotonic() + 20
        while _count(coordinator, "SELECT COUNT(*) FROM results WHERE job_id='job'") < 1:
            assert time.monotonic() < deadline
            await asyncio.sleep(0.002)
        await runner.pause("user")
        await asyncio.wait_for(sample, timeout=20)

    _run(second_phase())
    assert runner._state() is JobState.paused
    assert coordinator.store.rows("SELECT pause_reason FROM jobs WHERE id='job'")[0][0] == "user"


def test_on_result_exception_closes_attempt_and_phase_ends(
    coordinator: CoordinatorFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _real_pool(
        coordinator,
        tmp_path,
        monkeypatch,
        {"latency_ms": 20, "answers": {"10": {"ok": True, "call_number": "²"}}},
    )
    scenario_path = tmp_path / "fake-codex-scenario.json"
    scenario_path.write_text(
        json.dumps({"latency_ms": 20, "answers": {"10": {"ok": True, "call_number": chr(0x00B2)}}}),
        encoding="utf-8",
        newline="\n",
    )
    coordinator.runner.on_plan_approved(1)
    _run(coordinator.runner.run_sample())
    assert _count(coordinator, "SELECT COUNT(*) FROM leads WHERE job_id='job' AND status='in_flight'") == 0
    assert _count(coordinator, "SELECT COUNT(*) FROM attempts WHERE job_id='job' AND ended_at IS NULL") == 0


def test_on_result_raising_once_retries_or_fails_the_lead(
    coordinator: CoordinatorFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _real_pool(coordinator, tmp_path, monkeypatch, {"latency_ms": 20})
    original = runner_module._Callbacks.on_result
    raised = False

    async def raise_once(self: Any, task: Any, answer: Any, model_used: Any, usage: Any) -> None:
        nonlocal raised
        if not raised:
            raised = True
            raise RuntimeError("synthetic callback failure")
        await original(self, task, answer, model_used, usage)

    monkeypatch.setattr(runner_module._Callbacks, "on_result", raise_once)
    coordinator.runner.on_plan_approved(1)
    _run(coordinator.runner.run_sample())
    assert raised
    assert _count(coordinator, "SELECT COUNT(*) FROM attempts WHERE job_id='job' AND ended_at IS NULL") == 0
    assert _count(coordinator, "SELECT COUNT(*) FROM leads WHERE job_id='job' AND status='in_flight'") == 0


def test_appserver_crash_with_four_in_flight_restarts_once(
    coordinator: CoordinatorFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    crash_once = tmp_path / "crashed-once.txt"
    _real_pool(
        coordinator,
        tmp_path,
        monkeypatch,
        {"latency_ms": 50, "crash_after_turns": 1, "crash_once_file": str(crash_once)},
    )
    launches: list[str] = []
    original = pool_module.LunaPool._launch_and_prepare

    async def launch(self: LunaPool) -> None:
        launches.append("launch")
        await original(self)

    monkeypatch.setattr(pool_module.LunaPool, "_launch_and_prepare", launch)
    coordinator.runner.deps = replace(coordinator.runner.deps, concurrency=4)
    coordinator.runner.on_plan_approved(1)
    _run(coordinator.runner.run_sample())
    assert len(launches) == 2
    assert _count(coordinator, "SELECT COUNT(*) FROM results WHERE job_id='job'") == 4
    assert coordinator.runner._state() is JobState.sample_ready


def test_main_loop_waits_for_restart_window(
    coordinator: CoordinatorFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    crash_once = tmp_path / "crashed-once.txt"
    _real_pool(
        coordinator,
        tmp_path,
        monkeypatch,
        {"latency_ms": 50, "crash_after_turns": 1, "crash_once_file": str(crash_once)},
    )

    class SlowRestartPool(LunaPool):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["restart_backoff_s"] = 0.3
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(runner_module, "LunaPool", SlowRestartPool)
    launches: list[str] = []
    original = pool_module.LunaPool._launch_and_prepare

    async def launch(self: LunaPool) -> None:
        launches.append("launch")
        await original(self)

    monkeypatch.setattr(pool_module.LunaPool, "_launch_and_prepare", launch)
    coordinator.runner.deps = replace(coordinator.runner.deps, concurrency=4)
    coordinator.runner.on_plan_approved(1)
    _run(coordinator.runner.run_sample())

    assert len(launches) == 2
    assert _count(coordinator, "SELECT COUNT(*) FROM results WHERE job_id='job'") == 4
    assert coordinator.runner._state() is JobState.sample_ready


def test_sleep_with_parked_quota_pause_keeps_quota_deterministic(
    coordinator: CoordinatorFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class SleepOnQuotaDrainPool(LunaPool):
        clock: Any = None
        slept = False

        async def _pause(self, reason: str, resume_at: int | None) -> None:
            was_paused = self._paused
            await super()._pause(reason, resume_at)
            if (
                reason == "quota"
                and not was_paused
                and self._pending_pause is not None
                and not type(self).slept
            ):
                type(self).slept = True
                type(self).clock.current += 100

    scenario_path = _real_pool(
        coordinator,
        tmp_path,
        monkeypatch,
        {"used_percent": 94, "quota_increment": 1, "latency_ms": 150},
    )
    monkeypatch.setattr(runner_module, "LunaPool", SleepOnQuotaDrainPool)
    SleepOnQuotaDrainPool.clock = coordinator.clock
    SleepOnQuotaDrainPool.slept = False
    runner = coordinator.runner
    runner.on_plan_approved(1)
    _run(runner.run_sample())
    state, reason, resume_at = coordinator.store.rows(
        "SELECT state, pause_reason, resume_at FROM jobs WHERE id='job'"
    )[0]
    assert (state, reason) == ("paused", "quota")
    assert resume_at is not None
    assert any(event.kind == "sleep_detected" for event in coordinator.events.events)
    assert _count(coordinator, "SELECT COUNT(*) FROM leads WHERE job_id='job' AND status='held_back'") == 0
    assert (
        _count(coordinator, "SELECT COUNT(*) FROM job_events WHERE job_id='job' AND kind='stale_result'") == 0
    )
    coordinator.clock.current = float(resume_at)
    scenario_path.write_text(json.dumps({"latency_ms": 5}), encoding="utf-8", newline="\n")
    _run(runner.resume("automatic"))
    assert runner._state() is JobState.sample_ready
    assert _count(coordinator, "SELECT COUNT(*) FROM results WHERE job_id='job'") == 4


def test_user_pause_overrides_quota_pause(
    coordinator: CoordinatorFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _real_pool(
        coordinator,
        tmp_path,
        monkeypatch,
        {"used_percent": 94, "quota_increment": 1, "latency_ms": 20},
    )
    runner = coordinator.runner
    runner.on_plan_approved(1)
    _run(runner.run_sample())
    state, reason, resume_at = coordinator.store.rows(
        "SELECT state, pause_reason, resume_at FROM jobs WHERE id='job'"
    )[0]
    assert (state, reason) == ("paused", "quota")
    assert resume_at is not None

    _run(runner.pause("user"))
    row = coordinator.store.rows("SELECT pause_reason, resume_at FROM jobs WHERE id='job'")[0]
    assert tuple(row) == ("user", None)
    with pytest.raises(PermissionError):
        _run(runner.resume("automatic"))


def test_poison_lead_crashing_appserver_is_bounded(
    coordinator: CoordinatorFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kills: list[int] = []
    pools: list[LunaPool] = []
    launches: list[int] = []

    class PoisonPool(LunaPool):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["crash_window_s"] = 0.6
            super().__init__(*args, **kwargs)
            pools.append(self)

        async def _launch_and_prepare(self) -> None:
            launches.append(1)
            await super()._launch_and_prepare()

        async def _complete_from_events(self, lead: Any) -> None:
            server = self._server
            if lead.task.lead_id == "10" and server is not None and server.returncode is None:
                kills.append(1)
                server.process.kill()
            await super()._complete_from_events(lead)

    _real_pool(coordinator, tmp_path, monkeypatch, {"hangs": ["10"], "latency_ms": 20})
    monkeypatch.setattr(runner_module, "LunaPool", PoisonPool)
    coordinator.runner.deps = replace(coordinator.runner.deps, concurrency=4)
    coordinator.runner.on_plan_approved(1)
    _run(coordinator.runner.run_sample(), timeout=30)

    assert pools
    assert 2 <= len(launches) <= 5
    assert 1 <= len(kills) <= 4
    poison = tuple(
        coordinator.store.rows("SELECT status, reason FROM leads WHERE job_id='job' AND lead_id='10'")[0]
    )
    assert poison[0] in {"failed", "held_back"}
    assert poison[1] == "appserver_crash_repeated"
    assert (
        coordinator.store.rows(
            "SELECT COUNT(*) FROM attempts WHERE job_id='job' AND lead_id='10' "
            "AND error_code='appserver_crash_repeated' AND counted=1"
        )[0][0]
        >= 1
    )
    assert (
        coordinator.store.rows(
            "SELECT COUNT(*) FROM results r JOIN leads l ON l.job_id=r.job_id AND l.lead_id=r.lead_id "
            "WHERE r.job_id='job' AND r.lead_id <> '10' AND l.in_sample=1"
        )[0][0]
        == 3
    )
    assert coordinator.runner._state() in {JobState.sample_ready, JobState.paused}
    if coordinator.runner._state() is JobState.paused:
        assert coordinator.store.rows("SELECT pause_reason FROM jobs WHERE id='job'")[0][0] == "error_storm"


def test_failed_relaunch_pauses_job(
    coordinator: CoordinatorFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _real_pool(
        coordinator,
        tmp_path,
        monkeypatch,
        {"latency_ms": 20, "crash_after_turns": 1},
    )
    launches = 0
    original = pool_module.LunaPool._launch_and_prepare

    async def launch(self: LunaPool) -> None:
        nonlocal launches
        launches += 1
        if launches == 2:
            raise RuntimeError("synthetic relaunch failure")
        await original(self)

    monkeypatch.setattr(pool_module.LunaPool, "_launch_and_prepare", launch)
    coordinator.runner.on_plan_approved(1)
    _run(coordinator.runner.run_sample(), timeout=30)

    assert launches == 2
    assert coordinator.runner._state() is JobState.paused
    assert coordinator.store.rows("SELECT pause_reason FROM jobs WHERE id='job'")[0][0] == "error_storm"
    pool = coordinator.runner._phase_pool
    assert pool is not None
    assert not getattr(pool, "_leads", {})
