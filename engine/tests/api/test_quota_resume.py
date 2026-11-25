from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from bca.api.production import ProductionBackend
from bca.store import JobState, PauseReason, open_store
from bca.store.lifecycle import pause, resume
from tests.api.test_production import make_backend


class _Clock:
    def __init__(self, backend: ProductionBackend, now: float = 1_000.0) -> None:
        self.backend = backend
        self.value = now
        self.cycles = 0
        self.stop_after: int | None = None
        self.sleep_delta: float | None = None

    def now(self) -> float:
        return self.value

    def monotonic(self) -> float:
        return self.value

    async def sleep(self, seconds: float) -> None:
        self.value += self.sleep_delta if self.sleep_delta is not None else seconds
        self.cycles += 1
        await asyncio.sleep(0)
        if self.stop_after is not None and self.cycles >= self.stop_after:
            self.backend._stopping = True


def _job(backend: ProductionBackend, job_id: str = "job-one") -> None:
    backend.store.create_project(f"project-{job_id}", "Synthetic")
    backend.store.create_job(job_id, f"project-{job_id}", 1, JobState.full_running)


def _pause_job(
    backend: ProductionBackend,
    clock: _Clock,
    reason: PauseReason,
    resume_at: int | None = None,
    job_id: str = "job-one",
) -> None:
    _job(backend, job_id)
    pause(backend.store, job_id, reason, resume_at if resume_at is not None else int(clock.now()))


def _row(backend: ProductionBackend, job_id: str = "job-one") -> tuple[object, object, object]:
    result = backend.store.rows("SELECT state, pause_reason, resume_at FROM jobs WHERE id=?", (job_id,))
    return tuple(result[0])


async def _watch(backend: ProductionBackend) -> None:
    await asyncio.wait_for(backend._resume_watcher(), 10)


def _install_start(
    backend: ProductionBackend,
    resume_fn: Callable[[str], Awaitable[None]],
) -> list[tuple[str, float]]:
    calls: list[tuple[str, float]] = []

    async def start(job_id: str, operation: Callable[[Any], Awaitable[None]]) -> None:
        calls.append((job_id, backend.clock.now()))

        async def resume_automatic(_mode: str) -> None:
            await resume_fn(job_id)

        runner = cast(Any, SimpleNamespace(resume=resume_automatic))
        await operation(runner)

    backend.jobs.start = cast(Any, start)  # type: ignore[method-assign]
    return calls


def test_quota_pause_resumes_automatically_after_reset(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = make_backend(tmp_path)
        clock = _Clock(backend)
        backend.clock = clock
        _pause_job(backend, clock, PauseReason.quota, int(clock.now()) + 30)
        clock.sleep_delta = 15
        # Stopping is checked inside a cycle, so one full cycle needs a second sleep.
        clock.stop_after = 2
        await _watch(backend)
        assert _row(backend) == (JobState.paused, PauseReason.quota, 1_030)

        async def resume_once(job_id: str) -> None:
            await asyncio.sleep(0)
            resume(backend.store, job_id)
            backend._stopping = True

        calls = _install_start(backend, resume_once)
        backend._stopping = False
        clock.stop_after = None
        await _watch(backend)
        assert len(calls) == 1
        assert _row(backend) == (JobState.full_running, None, None)

    asyncio.run(asyncio.wait_for(scenario(), 10))


def test_refused_automatic_resume_waits_before_retrying(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = make_backend(tmp_path)
        clock = _Clock(backend)
        backend.clock = clock
        _pause_job(backend, clock, PauseReason.quota, int(clock.now()))
        attempts = 0

        async def resume_with_refusal(job_id: str) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise PermissionError("synthetic refusal")
            resume(backend.store, job_id)
            backend._stopping = True

        calls = _install_start(backend, resume_with_refusal)
        await _watch(backend)
        assert len(calls) == 2
        assert calls[1][1] - calls[0][1] >= 600
        assert _row(backend) == (JobState.full_running, None, None)

    asyncio.run(asyncio.wait_for(scenario(), 10))


@pytest.mark.parametrize(
    "reason",
    [
        PauseReason.user,
        PauseReason.sleep,
        PauseReason.credit,
        PauseReason.auth,
        PauseReason.model_unavailable,
        PauseReason.error_storm,
        PauseReason.network,
        PauseReason.spend_control,
        PauseReason.power,
    ],
)
def test_other_pause_reasons_never_resume_automatically(tmp_path: Path, reason: PauseReason) -> None:
    async def scenario() -> None:
        backend = make_backend(tmp_path)
        clock = _Clock(backend)
        backend.clock = clock
        _pause_job(backend, clock, reason, int(clock.now()) - 1)

        async def unexpected(_job_id: str) -> None:
            return None

        calls = _install_start(backend, unexpected)
        # Stopping is checked inside a cycle, so run several full cycles.
        clock.stop_after = 5
        before = _row(backend)
        await _watch(backend)
        assert calls == []
        assert _row(backend) == before

    asyncio.run(asyncio.wait_for(scenario(), 10))


def test_user_pause_after_automatic_resume_is_never_touched(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = make_backend(tmp_path)
        clock = _Clock(backend)
        backend.clock = clock
        _pause_job(backend, clock, PauseReason.quota, int(clock.now()))
        started = asyncio.Event()
        release = asyncio.Event()

        async def resume_then_wait(job_id: str) -> None:
            resume(backend.store, job_id)
            started.set()
            await release.wait()

        calls = _install_start(backend, resume_then_wait)
        watcher = asyncio.create_task(_watch(backend))
        await asyncio.wait_for(started.wait(), 10)
        pause(backend.store, "job-one", PauseReason.user)
        release.set()
        clock.stop_after = 50
        await watcher
        assert len(calls) == 1
        assert _row(backend) == (JobState.paused, PauseReason.user, None)

    asyncio.run(asyncio.wait_for(scenario(), 10))


def test_credit_pause_after_automatic_resume_is_never_touched(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = make_backend(tmp_path)
        clock = _Clock(backend)
        backend.clock = clock
        _pause_job(backend, clock, PauseReason.quota, int(clock.now()))
        started = asyncio.Event()
        release = asyncio.Event()

        async def resume_then_wait(job_id: str) -> None:
            resume(backend.store, job_id)
            started.set()
            await release.wait()

        calls = _install_start(backend, resume_then_wait)
        watcher = asyncio.create_task(_watch(backend))
        await asyncio.wait_for(started.wait(), 10)
        pause(backend.store, "job-one", PauseReason.credit)
        release.set()
        clock.stop_after = 50
        await watcher
        assert len(calls) == 1
        assert _row(backend) == (JobState.paused, PauseReason.credit, None)

    asyncio.run(asyncio.wait_for(scenario(), 10))


def test_new_quota_pause_keeps_its_own_reset_time(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = make_backend(tmp_path)
        clock = _Clock(backend)
        backend.clock = clock
        _pause_job(backend, clock, PauseReason.quota, int(clock.now()))
        later = int(clock.now()) + 1_000

        async def resume_and_repause(job_id: str) -> None:
            resume(backend.store, job_id)
            pause(backend.store, job_id, PauseReason.quota, later)

        _install_start(backend, resume_and_repause)
        clock.stop_after = 2
        await _watch(backend)
        assert _row(backend) == (JobState.paused, PauseReason.quota, later)

    asyncio.run(asyncio.wait_for(scenario(), 10))


def test_second_due_job_is_resumed_while_the_first_is_running(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = make_backend(tmp_path)
        clock = _Clock(backend)
        backend.clock = clock
        _pause_job(backend, clock, PauseReason.quota, int(clock.now()), "job-one")
        _pause_job(backend, clock, PauseReason.quota, int(clock.now()), "job-two")
        first_running = asyncio.Event()
        release_first = asyncio.Event()
        running: list[asyncio.Task[None]] = []
        calls: list[str] = []

        async def start(job_id: str, operation: Callable[[Any], Awaitable[None]]) -> None:
            calls.append(job_id)

            async def resume_and_wait(_mode: str, _job_id: str = job_id) -> None:
                resume(backend.store, _job_id)
                if _job_id == "job-one":
                    first_running.set()
                    await release_first.wait()
                else:
                    backend._stopping = True

            runner = cast(Any, SimpleNamespace(resume=resume_and_wait))
            task: asyncio.Task[None] = asyncio.create_task(cast(Coroutine[Any, Any, None], operation(runner)))
            running.append(task)
            backend.jobs.tasks[job_id] = cast(Any, task)
            await asyncio.sleep(0)

        backend.jobs.start = cast(Any, start)  # type: ignore[method-assign]
        watcher = asyncio.create_task(_watch(backend))
        await asyncio.wait_for(first_running.wait(), 10)
        for _ in range(200):
            if "job-two" in calls:
                break
            await asyncio.sleep(0)
        assert calls == ["job-one", "job-two"]
        release_first.set()
        await watcher
        await asyncio.gather(*running)
        assert _row(backend, "job-one") == (JobState.full_running, None, None)
        assert _row(backend, "job-two") == (JobState.full_running, None, None)

    asyncio.run(asyncio.wait_for(scenario(), 10))


def test_watcher_survives_a_store_error(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = make_backend(tmp_path)
        clock = _Clock(backend)
        backend.clock = clock
        _pause_job(backend, clock, PauseReason.quota, int(clock.now()))
        original_rows = backend.store.rows
        failed = False

        def rows(query: str, args: tuple[object, ...] = ()) -> list[Any]:
            nonlocal failed
            if not failed and query.startswith("SELECT id, resume_at FROM jobs"):
                failed = True
                raise RuntimeError("synthetic store error")
            return original_rows(query, args)

        object.__setattr__(backend.store, "rows", cast(Any, rows))

        async def resume_once(job_id: str) -> None:
            resume(backend.store, job_id)
            backend._stopping = True

        calls = _install_start(backend, resume_once)
        await _watch(backend)
        assert len(calls) == 1
        assert _row(backend) == (JobState.full_running, None, None)

    asyncio.run(asyncio.wait_for(scenario(), 10))


def test_quota_pause_resumes_after_engine_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        template = make_backend(tmp_path)
        config = template.config
        template.store.close()
        store_path = tmp_path / "store.sqlite3"
        first_clock = _Clock(template)
        first = ProductionBackend(tmp_path, config=config, store=open_store(store_path), clock=first_clock)
        first_clock.backend = first
        _pause_job(first, first_clock, PauseReason.quota, int(first_clock.now()) - 1)
        first.store.close()

        second = ProductionBackend(tmp_path, config=config, store=open_store(store_path), clock=_Clock(first))
        cast(_Clock, second.clock).backend = second

        async def resume_once(job_id: str) -> None:
            resume(second.store, job_id)
            second._stopping = True

        calls = _install_start(second, resume_once)
        await _watch(second)
        assert len(calls) == 1
        assert _row(second) == (JobState.full_running, None, None)

    asyncio.run(asyncio.wait_for(scenario(), 10))
