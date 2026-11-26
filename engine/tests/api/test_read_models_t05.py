from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import pytest
from openpyxl import load_workbook

from bca.api.server import RpcError
from bca.store.lifecycle import JobState, PauseReason, transition
from tests.api.t04_lifecycle import (
    JOB,
    Lifecycle,
    _wait_for_lead_started,
    _wait_state,
    run_lifecycle,
    run_lifecycle_sync,
)


@pytest.fixture(scope="module")
def full_lifecycle(tmp_path_factory: pytest.TempPathFactory) -> Lifecycle:
    return run_lifecycle_sync(tmp_path_factory.mktemp("full"))


def run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


def test_full_phase_progress_counts_every_lead(full_lifecycle: Lifecycle) -> None:
    progress_messages = [
        cast(dict[str, object], message["params"])
        for message in full_lifecycle.notifications
        if message["method"] == "job/progress"
    ]
    assert progress_messages
    assert {item["total"] for item in progress_messages if item["phase"] == "full"} == {8}
    assert {item["total"] for item in progress_messages if item["phase"] == "sample"} == {2}
    first_full = next(item for item in progress_messages if item["phase"] == "full")
    assert cast(int, first_full["done"]) >= 2
    for item in progress_messages:
        assert (
            sum(
                cast(int, item[key])
                for key in ("done", "inFlight", "retrying", "failed", "heldBack", "remaining")
            )
            == item["total"]
        )

    for snapshot in (full_lifecycle.running, full_lifecycle.paused, full_lifecycle.paused_quota):
        progress = cast(dict[str, object], snapshot["progress"])
        assert progress["total"] == 8
        assert (
            sum(
                cast(int, progress[key])
                for key in ("done", "inFlight", "retrying", "failed", "heldBack", "remaining")
            )
            == progress["total"]
        )


def test_paused_and_stalled_jobs_keep_their_steps(full_lifecycle: Lifecycle, tmp_path: Path) -> None:
    for snapshot in (full_lifecycle.paused, full_lifecycle.paused_quota):
        steps = cast(list[dict[str, object]], snapshot["steps"])
        assert steps[7]["status"] == "active"
        assert all(step["status"] == "done" for step in steps[:7])

    stalled_path = tmp_path / "stalled"
    stalled_path.mkdir()
    lifecycle = run_lifecycle_sync(stalled_path, start_full=False)

    async def stall() -> dict[str, object]:
        await lifecycle.backend.call("job/startFull", {"job": JOB})
        for _ in range(6000):
            state = lifecycle.backend.store.rows("SELECT state FROM jobs WHERE id=?", (JOB,))[0][0]
            if state == JobState.full_running.value:
                transition(lifecycle.backend.store, JOB, JobState.stalled, "synthetic stall")
                break
            await asyncio.sleep(0.01)
        return cast(dict[str, object], await lifecycle.backend.call("job/snapshot", {"job": JOB}))

    stalled = run(stall())
    steps = cast(list[dict[str, object]], stalled["steps"])
    assert steps[7]["status"] == "active"
    assert all(step["status"] == "done" for step in steps[:7])


def test_sample_workbook_is_built_at_sample_ready(full_lifecycle: Lifecycle) -> None:
    sample_file = full_lifecycle.sample["file"]
    assert sample_file == "jobs/job-one/output/sample.v1.xlsx"
    resolved = cast(
        dict[str, object],
        run(full_lifecycle.backend.call("files/path", {"project": "project-one", "name": sample_file})),
    )
    workbook = load_workbook(str(resolved["path"]), read_only=True, data_only=False)
    try:
        sheet = workbook["Results"]
        rows = list(sheet.iter_rows(values_only=True))
        headers = [str(value) for value in rows[0]]
        assert {str(row[0]) for row in rows[1:]} == {"1000", "1001"}
        output_columns = cast(list[dict[str, object]], full_lifecycle.plan["outputColumns"])
        column_names = {str(column["name"]) for column in output_columns}
        assert "answer" in column_names
        assert column_names <= set(headers)
    finally:
        workbook.close()


def test_sample_workbook_failure_does_not_block_sample_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: object, **kwargs: object) -> Path:
        raise RuntimeError("synthetic workbook failure")

    monkeypatch.setattr("bca.coordinator.runner.build_sample_workbook", fail)
    lifecycle = run_lifecycle_sync(tmp_path, start_full=False)
    assert lifecycle.sample["file"] is None
    assert lifecycle.sample["job"] == JOB
    # The helper waits for sample_ready (it would time out otherwise), reads job/sample there, then approves.
    assert (
        lifecycle.backend.store.rows("SELECT state FROM jobs WHERE id=?", (JOB,))[0][0] == "sample_approved"
    )


def test_snapshot_quota_has_stop_percent(full_lifecycle: Lifecycle) -> None:
    quota = cast(dict[str, object], full_lifecycle.running["quota"])
    codex = cast(dict[str, object], quota["codex"])
    assert codex["stopAtPercent"] == full_lifecycle.backend.config.limits.codex_stop_percent


def test_resume_while_draining_has_a_reason(tmp_path: Path) -> None:
    async def scenario() -> None:
        # One event loop for the whole test: the job's driver task must not outlive the loop that runs it.
        lifecycle = await run_lifecycle(tmp_path, start_full=False)
        backend = lifecycle.backend
        await backend.call("job/startFull", {"job": JOB})
        await _wait_for_lead_started(backend)
        # A quota stop as the pool raises it: the state is paused at once while the driver still
        # drains the leads in flight (each takes 3 s in the fake). A user pause waits for the drain.
        runner = backend.jobs.runners[JOB]
        resume_at = int(backend.clock.now()) + 3600
        await runner._pause_with_pool(PauseReason.quota.value, resume_at, wait_for_phase=False)
        await _wait_state(backend, {"paused"})
        try:
            with pytest.raises(RpcError) as error:
                await backend.call("job/resume", {"job": JOB})
            assert error.value.code == -32001
            assert cast(dict[str, object], error.value.data)["reason"] == "job_running"
        finally:
            task = backend.jobs.tasks.get(JOB)
            if task is not None:
                await asyncio.wait_for(asyncio.shield(task), timeout=60)

    run(asyncio.wait_for(scenario(), timeout=120))
