"""Concurrency, pause and sleep regressions, each driven through its named scenario.

Every test uses the REAL LunaPool with the fake app-server (tests/fakes/fake_codex.py),
instrumented to count concurrent run_until_idle calls and to track every app-server it
starts. Each scenario runs on ONE event loop, as the engine does, under a hard timeout.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

import bca.coordinator.runner as runner_module
from bca.luna.pool import LunaPool
from bca.store import JobState
from tests.coordinator.conftest import CoordinatorFixture
from tests.coordinator.test_pool_quota import _real_pool

REC: dict[str, Any] = {}


def _reset() -> None:
    REC.clear()
    REC.update({"depth": 0, "max_depth": 0, "launches": 0, "servers": []})


class RecPool(LunaPool):
    """The real pool, plus counters for overlapping runs and the app-servers it starts."""

    async def run_until_idle(self) -> None:
        REC["depth"] += 1
        REC["max_depth"] = max(REC["max_depth"], REC["depth"])
        try:
            await super().run_until_idle()
        finally:
            REC["depth"] -= 1

    async def _launch_and_prepare(self) -> None:
        REC["launches"] += 1
        await super()._launch_and_prepare()
        if self._server is not None:
            REC["servers"].append(self._server)


class SleepOnQuotaDrainPool(RecPool):
    """When the pool parks a quota pause because leads are still in flight, the laptop sleeps."""

    clock: Any = None
    slept = False

    async def _pause(self, reason: str, resume_at: int | None) -> None:
        had = self._paused
        await super()._pause(reason, resume_at)
        # The laptop sleeps ONCE, while the first parked quota drain is still running.
        if reason == "quota" and not had and self._pending_pause is not None and not type(self).slept:
            type(self).slept = True
            type(self).clock.current += 100


def _setup(
    coordinator: CoordinatorFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: dict[str, object],
    pool: type[LunaPool] = RecPool,
) -> Path:
    path = _real_pool(coordinator, tmp_path, monkeypatch, scenario)
    monkeypatch.setattr(runner_module, "LunaPool", pool)
    _reset()
    return path


def _run(factory: Callable[[], Awaitable[Any]], timeout: float = 30) -> Any:
    async def bounded() -> Any:
        return await asyncio.wait_for(factory(), timeout)

    return asyncio.run(bounded())


async def _wait(predicate: Callable[[], bool], limit: float = 15) -> None:
    deadline = time.monotonic() + limit
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.005)


def _count(coordinator: CoordinatorFixture, sql: str, args: tuple[object, ...] = ()) -> int:
    return int(coordinator.store.rows(sql, args)[0][0])


def _job(coordinator: CoordinatorFixture) -> tuple[Any, ...]:
    return tuple(coordinator.store.rows("SELECT state, pause_reason, resume_at FROM jobs WHERE id='job'")[0])


def _assert_clean(coordinator: CoordinatorFixture) -> None:
    """No overlapping runs, no held-back sample lead, no stale result, no orphan claim, no live server."""
    assert REC["max_depth"] <= 1
    assert (
        _count(
            coordinator,
            "SELECT COUNT(*) FROM leads WHERE job_id='job' AND in_sample=1 AND status='held_back'",
        )
        == 0
    )
    assert (
        _count(coordinator, "SELECT COUNT(*) FROM job_events WHERE job_id='job' AND kind='stale_result'") == 0
    )
    assert (
        _count(
            coordinator,
            "SELECT COUNT(*) FROM leads l WHERE job_id='job' AND status='in_flight' AND NOT EXISTS "
            "(SELECT 1 FROM attempts a WHERE a.job_id=l.job_id AND a.lead_id=l.lead_id "
            "AND a.ended_at IS NULL)",
        )
        == 0
    )
    assert [server for server in REC["servers"] if server.returncode is None] == []


def _sample_results(coordinator: CoordinatorFixture) -> int:
    return _count(
        coordinator,
        "SELECT COUNT(*) FROM results r JOIN leads l ON l.job_id=r.job_id AND l.lead_id=r.lead_id "
        "WHERE r.job_id='job' AND l.in_sample=1",
    )


def _jump_clock_on_first_result(
    coordinator: CoordinatorFixture, monkeypatch: pytest.MonkeyPatch, sample: bool | None = None
) -> None:
    """The wall clock jumps 100 s after the first answer while monotonic time does not: a sleep gap."""
    original = runner_module._Callbacks.on_result
    seen = {"n": 0}

    async def on_result(self: Any, task: Any, answer: Any, model_used: Any, usage: Any) -> None:
        await original(self, task, answer, model_used, usage)
        if sample is not None and self.sample != sample:
            return
        seen["n"] += 1
        if seen["n"] == 1:
            coordinator.clock.current += 100

    monkeypatch.setattr(runner_module._Callbacks, "on_result", on_result)


def test_pause_then_quick_resume_runs_one_phase_at_a_time(
    coordinator: CoordinatorFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(coordinator, tmp_path, monkeypatch, {"latency_ms": 400})
    runner = coordinator.runner
    runner.on_plan_approved(1)

    async def scenario() -> None:
        sample = asyncio.create_task(runner.run_sample())
        await _wait(lambda: _count(coordinator, "SELECT COUNT(*) FROM attempts WHERE job_id='job'") >= 2)
        pause = asyncio.create_task(runner.pause("user"))
        await asyncio.sleep(0.05)
        await runner.resume("operator")
        await pause
        await sample

    _run(scenario)
    assert runner._state() is JobState.sample_ready
    assert _sample_results(coordinator) == 4
    _assert_clean(coordinator)


def test_resume_during_model_unavailable_drain(
    coordinator: CoordinatorFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(
        coordinator,
        tmp_path,
        monkeypatch,
        {"reroute": ["10"], "models": ["codex", "codex-mini"], "latency_ms": 400},
    )
    runner = coordinator.runner
    runner.on_plan_approved(1)

    async def scenario() -> None:
        sample = asyncio.create_task(runner.run_sample())
        await _wait(lambda: runner._state() is JobState.paused)
        # The graceful model_unavailable pause keeps the in-flight turns running: resume mid-drain.
        assert _count(coordinator, "SELECT COUNT(*) FROM leads WHERE job_id='job' AND status='in_flight'") > 0
        await runner.resume("operator")
        await sample

    _run(scenario)
    assert runner._state() is JobState.sample_ready
    assert _sample_results(coordinator) == 4
    rerouted = coordinator.store.rows(
        "SELECT model_used, rerouted_from FROM attempts WHERE job_id='job' AND lead_id='10' AND outcome='ok'"
    )
    assert [tuple(row) for row in rerouted] == [("codex-mini", "codex")]
    _assert_clean(coordinator)


def test_parked_quota_pause_after_runner_pause_does_not_raise(
    coordinator: CoordinatorFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The quota passes the stop line after the first answer, so the pool parks its quota pause while
    # leads are still in flight; lead 12's reroute then makes the runner pause first. The parked
    # pause is delivered afterwards, from run_until_idle's finally.
    _setup(
        coordinator,
        tmp_path,
        monkeypatch,
        {
            "used_percent": 94,
            "quota_increment": 1,
            "reroute": ["12"],
            "models": ["codex", "codex-mini"],
            "latency_ms": 60,
        },
    )
    runner = coordinator.runner
    runner.on_plan_approved(1)

    _run(runner.run_sample)

    assert runner._state() is JobState.paused
    assert REC["launches"] >= 1
    _assert_clean(coordinator)


def test_network_bound_during_model_unavailable_drain_does_not_raise(
    coordinator: CoordinatorFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(
        coordinator,
        tmp_path,
        monkeypatch,
        {
            "reroute": ["10"],
            "models": ["codex", "codex-mini"],
            "errors": {"11": "rateLimitExceeded"},
            "latency_ms": 300,
        },
    )
    runner = coordinator.runner
    runner.on_plan_approved(1)
    # Lead 11 already has 3 uncounted network failures, so its next one reaches the bound
    # while the model_unavailable drain is still running.
    for _ in range(3):
        attempt = coordinator.store.start_attempt("job", "11", "codex")
        coordinator.store.fail_attempt(attempt, "httpConnectionFailed", False, 2)

    _run(runner.run_sample)

    assert runner._state() is JobState.paused
    assert _job(coordinator)[1] in {"model_unavailable", "network"}
    _assert_clean(coordinator)


def test_sleep_gap_during_quota_drain_holds_back_nothing(
    coordinator: CoordinatorFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _setup(
        coordinator,
        tmp_path,
        monkeypatch,
        {"used_percent": 94, "quota_increment": 1, "latency_ms": 150},
        SleepOnQuotaDrainPool,
    )
    SleepOnQuotaDrainPool.clock = coordinator.clock
    SleepOnQuotaDrainPool.slept = False
    runner = coordinator.runner
    runner.on_plan_approved(1)

    _run(runner.run_sample)

    # The sleep must not replace the quota pause: the reset time is kept for the automatic resume.
    state, reason, resume_at = _job(coordinator)
    assert (state, reason) == ("paused", "quota")
    assert resume_at is not None
    _assert_clean(coordinator)

    coordinator.clock.current = float(resume_at)
    path.write_text(json.dumps({"latency_ms": 5}), encoding="utf-8", newline="\n")
    _run(lambda: runner.resume("automatic"))
    assert runner._state() is JobState.sample_ready
    assert _sample_results(coordinator) == 4
    _assert_clean(coordinator)


def test_sample_sleep_gap_all_sample_leads_get_results(
    coordinator: CoordinatorFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(coordinator, tmp_path, monkeypatch, {"latency_ms": 300})
    _jump_clock_on_first_result(coordinator, monkeypatch)
    runner = coordinator.runner
    runner.on_plan_approved(1)

    # No manual resume: the runner resumes by itself on wake.
    _run(runner.run_sample)

    assert runner._state() is JobState.sample_ready
    assert _sample_results(coordinator) == 4
    kinds = [event.kind for event in coordinator.events.events]
    assert "sleep_detected" in kinds
    # Turns interrupted by the sleep were requeued uncounted: one counted attempt per sample lead.
    assert (
        _count(
            coordinator,
            "SELECT MAX(c) FROM (SELECT COUNT(*) c FROM attempts a JOIN leads l ON l.job_id=a.job_id "
            "AND l.lead_id=a.lead_id WHERE a.job_id='job' AND l.in_sample=1 AND a.counted=1 "
            "GROUP BY a.lead_id)",
        )
        == 1
    )
    _assert_clean(coordinator)


def test_full_run_sleep_gap_then_inflight_error_completes(
    coordinator: CoordinatorFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _setup(coordinator, tmp_path, monkeypatch, {"latency_ms": 1})
    runner = coordinator.runner
    runner.on_plan_approved(1)
    _run(runner.run_sample)
    runner.on_sample_approved(1)
    path.write_text(json.dumps({"latency_ms": 200}), encoding="utf-8", newline="\n")
    _jump_clock_on_first_result(coordinator, monkeypatch, sample=False)

    # After the wake, lead 15's turn fails once in flight (a retryable, uncounted stream error).
    on_result = runner_module._Callbacks.on_result
    failed_once = {"done": False}

    async def fail_lead_15_once(self: Any, task: Any, answer: Any, model_used: Any, usage: Any) -> None:
        if task.lead_id == "15" and not failed_once["done"]:
            failed_once["done"] = True
            await self.on_failure(task, "responseStreamDisconnected", False, "synthetic in-flight error")
            return
        await on_result(self, task, answer, model_used, usage)

    monkeypatch.setattr(runner_module._Callbacks, "on_result", fail_lead_15_once)

    _run(runner.start_full_run, 45)

    assert failed_once["done"]
    assert runner._state() in {JobState.done, JobState.done_with_issues}
    lead_15 = coordinator.store.rows(
        "SELECT outcome, counted FROM attempts WHERE job_id='job' AND lead_id='15' ORDER BY started_at, rowid"
    )
    assert tuple(lead_15[-1]) == ("ok", 1)
    assert (0,) in [(int(row[1]),) for row in lead_15 if str(row[0]) != "ok"]
    _assert_clean(coordinator)


def test_pause_while_phase_loop_idle_never_relaunches_pool(
    coordinator: CoordinatorFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(coordinator, tmp_path, monkeypatch, {"latency_ms": 300})
    runner = coordinator.runner
    runner.on_plan_approved(1)

    async def scenario() -> None:
        sample = asyncio.create_task(runner.run_sample())
        await _wait(lambda: _count(coordinator, "SELECT COUNT(*) FROM results WHERE job_id='job'") >= 1)
        await runner.pause("user")
        launches = REC["launches"]
        attempts = _count(coordinator, "SELECT COUNT(*) FROM attempts WHERE job_id='job'")
        # Longer than the phase loop's 1 s idle wait: a loop that ignored the pause would relaunch.
        await asyncio.sleep(1.5)
        assert runner._state() is JobState.paused
        assert REC["launches"] == launches
        assert _count(coordinator, "SELECT COUNT(*) FROM attempts WHERE job_id='job'") == attempts
        await sample

    _run(scenario)
    assert runner._state() is JobState.paused
    _assert_clean(coordinator)
