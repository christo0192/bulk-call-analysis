from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from bca.api.production import ProductionBackend
from bca.api.server import ApiService
from bca.bq.runner import DryRun
from bca.mcp.host import ToolHost
from bca.store import CapRule, JobSpec, LeadSourceSql, UsableRule
from bca.store.lifecycle import PauseReason
from tests.api.test_production import make_backend
from tests.fakes.fake_bq import FakeBqRunner

JOB = "job-one"
PROJECT = "project-one"


@dataclass
class Lifecycle:
    backend: ProductionBackend
    plan: dict[str, object]
    plan_snapshot: dict[str, object]
    sample: dict[str, object]
    running: dict[str, object]
    paused: dict[str, object]
    outputs: dict[str, object]
    paused_quota: dict[str, object] = field(default_factory=dict)
    notifications: list[dict[str, object]] = field(default_factory=list)


@asynccontextmanager
async def captured_notifications(backend: ProductionBackend) -> AsyncIterator[list[dict[str, object]]]:
    """Collect what the real server layer would send to the app while the block runs."""
    sent: list[dict[str, object]] = []
    service = ApiService(backend, backend.event_bus)

    async def notify(message: dict[str, object]) -> None:
        sent.append(message)

    service.notify = notify
    subscription = backend.event_bus.subscribe()

    async def pump() -> None:
        while True:
            for event in subscription.drain():
                await service.publish_event(event)
            await asyncio.sleep(0.01)

    task = asyncio.create_task(pump())
    try:
        yield sent
        await asyncio.sleep(0.05)
        for event in subscription.drain():
            await service.publish_event(event)
    finally:
        task.cancel()
        backend.event_bus.unsubscribe(subscription)


def _host(backend: ProductionBackend) -> ToolHost:
    return ToolHost(
        runner=backend._make_bq(),
        store=backend.store,
        project_dir=backend._project_dir(PROJECT),
        bq_config=backend.config.bigquery,
        mask_context=backend._make_masker()(backend._project_dir(PROJECT)),
        project_id=PROJECT,
        recipe=backend.config.recipe,
        models=backend.config.models,
    )


def _rows() -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    leads: list[dict[str, object]] = [{"lead_id": str(1000 + index)} for index in range(8)]
    preflight = [
        {
            "lead_id": str(lead["lead_id"]),
            "found": True,
            "calls": 1,
            "transcript_calls": 1,
            "usable_calls": 1,
            "transcription_failed": 0,
            "transcript_chars": 80,
        }
        for lead in leads
    ]
    calls = [
        {
            "lead_id": str(lead["lead_id"]),
            "call_id": f"call-{lead['lead_id']}-synthetic",
            "call_start": "2026-09-01T10:00:00+00:00",
            "duration": 600_000,
            "transcript_text": f"Synthetic transcript for lead {lead['lead_id']}",
        }
        for lead in leads
    ]
    return leads, preflight, calls


async def _status(backend: ProductionBackend) -> dict[str, object]:
    return cast(dict[str, object], await backend.call("job/status", {"job": JOB}))


async def _snapshot(backend: ProductionBackend) -> dict[str, object]:
    return cast(dict[str, object], await backend.call("job/snapshot", {"job": JOB}))


async def _wait_state(backend: ProductionBackend, expected: set[str]) -> None:
    async def wait() -> None:
        while True:
            status = await _status(backend)
            if status["state"] in expected:
                return
            await asyncio.sleep(0.01)

    try:
        await asyncio.wait_for(wait(), timeout=60)
    except TimeoutError as exc:
        status = await _snapshot(backend)
        raise AssertionError(
            f"job did not reach {expected}: state={status.get('state')}, step={status.get('step')}"
        ) from exc


async def _wait_for_lead_started(backend: ProductionBackend) -> None:
    subscription = backend.event_bus.subscribe()
    try:

        async def wait() -> None:
            while True:
                for event in subscription.drain():
                    if event.job_id == JOB and event.kind == "lead_started":
                        return
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait(), timeout=60)
    except TimeoutError as exc:
        status = await _snapshot(backend)
        raise AssertionError(
            f"job did not emit lead_started: state={status.get('state')}, step={status.get('step')}"
        ) from exc
    finally:
        backend.event_bus.unsubscribe(subscription)


async def _wait_for_codex_activity(backend: ProductionBackend) -> None:
    """Wait until the running phase has seen a Codex notification (the fake sends turn/started at once).

    Without this, whether a live snapshot's lastActivityTs is null or a time depends on machine
    speed, and the recorded fixtures differ between runs.
    """

    async def wait() -> None:
        while backend.jobs.runners[JOB]._last_activity_ts is None:
            await asyncio.sleep(0.01)

    try:
        await asyncio.wait_for(wait(), timeout=60)
    except TimeoutError as exc:
        raise AssertionError("the running phase saw no Codex activity") from exc


async def run_lifecycle(tmp_path: Path, *, latency_ms: int = 3000, start_full: bool = True) -> Lifecycle:
    (tmp_path / ".bca-test-fakes").touch()
    os.environ["BCA_TEST_FAKES"] = "1"
    leads, preflight, calls = _rows()
    bq = FakeBqRunner(
        pages=[calls],
        preflight_pages=[preflight],
        pages_by_query_kind={"lead_source": [leads]},
        dry=DryRun(1, [], [("lead_id", "STRING")]),
    )
    backend = make_backend(tmp_path, bq=bq)
    await backend.call("projects/create", {"project": PROJECT, "title": "Synthetic"})
    host = _host(backend)
    spec = JobSpec(
        1,
        JOB,
        PROJECT,
        "Synthetic job",
        "Synthetic question",
        LeadSourceSql("SELECT '1000' AS lead_id"),
        usable=UsableRule(min_chars=1),
        cap=CapRule(100, "drop_short_then_oldest", 180),
        instructions="Return JSON.",
        output_schema={"type": "object", "properties": {"answer": {"type": "string"}}},
        luna_model=backend.config.models.luna_model,
        luna_effort=backend.config.models.luna_effort,
        sample_size=2,
    )
    card = cast(dict[str, object], host.call("propose_job", {"spec_json": json.dumps(spec.to_dict())}))
    job_dir = tmp_path / "projects" / PROJECT / "jobs" / JOB
    (job_dir / "fake-codex-scenario.json").write_text(
        json.dumps(
            {
                "default_answer": {"answer": "synthetic"},
                "latency_ms": latency_ms,
                "models": [backend.config.models.luna_model],
            }
        ),
        encoding="utf-8",
        newline="\n",
    )
    plan = cast(dict[str, object], await backend.call("job/plan", {"job": JOB}))
    plan_snapshot = cast(dict[str, object], await backend.call("job/snapshot", {"job": JOB}))
    async with captured_notifications(backend) as sample_sent:
        await backend.call(
            "job/approvePlan", {"job": JOB, "expectedSpecVersion": int(cast(int, card["spec_version"]))}
        )
        await _wait_state(backend, {"sample_ready"})
        sample = cast(dict[str, object], await backend.call("job/sample", {"job": JOB}))
        await backend.call("job/approveSample", {"job": JOB, "expectedSpecVersion": 1})
    if not start_full:
        return Lifecycle(backend, plan, plan_snapshot, sample, {}, {}, {})

    async with captured_notifications(backend) as sent:
        await backend.call("job/startFull", {"job": JOB})
        await _wait_for_lead_started(backend)
        await _wait_for_codex_activity(backend)
        running = await _snapshot(backend)
        await backend.call("job/pause", {"job": JOB})
        await _wait_state(backend, {"paused"})
        paused = await _snapshot(backend)
        await backend.call("job/resume", {"job": JOB})
        await _wait_for_lead_started(backend)
        await _wait_for_codex_activity(backend)
        # A quota stop as the pool raises it: a reset time one hour ahead.
        runner = backend.jobs.runners[JOB]
        resume_at = int(backend.clock.now()) + 3600
        await runner._pause_with_pool(PauseReason.quota.value, resume_at, wait_for_phase=False)
        await _wait_state(backend, {"paused"})
        paused_quota = await _snapshot(backend)
        # Resume is refused until the paused driver has drained its leads in flight.
        await asyncio.wait_for(asyncio.shield(backend.jobs.tasks[JOB]), timeout=60)
        await backend.call("job/resume", {"job": JOB})
        await _wait_state(backend, {"done", "done_with_issues"})
    outputs = cast(dict[str, object], await backend.call("job/outputs", {"job": JOB}))
    assert isinstance(outputs["finishedAt"], (int, float))
    assert isinstance(outputs["durationSeconds"], (int, float))
    assert running["progress"] and running["steps"] and running["activity"]
    assert cast(dict[str, object], running["activity"])["workers"]
    return Lifecycle(
        backend, plan, plan_snapshot, sample, running, paused, outputs, paused_quota, sample_sent + sent
    )


def run_lifecycle_sync(tmp_path: Path, *, latency_ms: int = 3000, start_full: bool = True) -> Lifecycle:
    return asyncio.run(run_lifecycle(tmp_path, latency_ms=latency_ms, start_full=start_full))
