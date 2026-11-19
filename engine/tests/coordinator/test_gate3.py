from __future__ import annotations

import asyncio
from dataclasses import replace
from functools import partial
from typing import Any

import pytest

import bca.coordinator.runner as runner_module
from bca.mcp.host import ToolHostError
from bca.store import JobState, LeadStatus, edit_spec, save_spec
from tests.coordinator.conftest import DeterministicPool
from tests.coordinator.test_faults import Script, ScriptedPool
from tests.mcp.test_tools import proposal_host, proposal_json


def test_bundle_cache_keeps_metadata_not_text(coordinator: Any) -> None:
    coordinator.runner.on_plan_approved(1)
    asyncio.run(asyncio.wait_for(coordinator.runner.run_sample(), timeout=15))

    assert all(value[1] is None for value in coordinator.runner._bundles.values())
    assert coordinator.runner._state() is JobState.sample_ready


def test_phase_stops_when_store_state_changes(coordinator: Any) -> None:
    coordinator.runner.on_plan_approved(1)
    coordinator.store.connection.execute(
        "UPDATE jobs SET state='plan_proposed', resume_state=NULL WHERE id='job'"
    )
    coordinator.store.connection.commit()

    asyncio.run(asyncio.wait_for(coordinator.runner.run_sample(), timeout=15))
    assert coordinator.runner._state() is JobState.plan_proposed


def test_host_refuses_plan_edit_while_phase_running(tmp_path: Any) -> None:
    host = proposal_host(tmp_path, [])
    # The job already exists with its sample running; no BigQuery is needed to reach the refusal.
    host.store.create_job("job", host.project_id, 1, JobState.sample_running)
    with pytest.raises(ToolHostError, match=r"Pause the job before editing its plan\."):
        host.call("propose_job", {"spec_json": proposal_json("edited")})
    assert host.store.rows("SELECT spec_version FROM jobs WHERE id='job'")[0][0] == 1


def test_plan_edit_while_paused_then_reapprove_runs_full_v2_sample(coordinator: Any) -> None:
    updated = replace(coordinator.runner.spec, spec_version=2, instructions="Return v2.")
    save_spec(updated, coordinator.job_dir)
    edit_spec(coordinator.store, "job", 2)
    coordinator.runner.on_plan_approved(2)
    asyncio.run(asyncio.wait_for(coordinator.runner.run_sample(), timeout=15))
    rows = coordinator.store.rows(
        "SELECT status FROM leads WHERE job_id='job' AND in_sample=1 ORDER BY lead_id"
    )
    assert [str(row[0]) for row in rows] == [LeadStatus.done] * 4
    assert (
        coordinator.store.rows("SELECT COUNT(*) FROM job_events WHERE job_id='job' AND kind='stale_result'")[
            0
        ][0]
        == 0
    )


def test_over_cap_hold_back_is_reevaluated_after_cap_raise(
    coordinator: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    held_back_lead = "20"
    lead_calls = [call for call in coordinator.bq.pages[0] if str(call["lead_id"]) == held_back_lead]
    assert lead_calls
    for call in lead_calls:
        call["transcript_text"] = "Synthetic Name says hello. " + ("x" * 500)

    # Lead 20 is a full-run lead: background masking bundles it during the v1 sample and holds it
    # back because every call exceeds the v1 cap (asyncio.run waits for the masking thread).
    coordinator.runner.on_plan_approved(1)
    asyncio.run(asyncio.wait_for(coordinator.runner.run_sample(), timeout=15))
    assert coordinator.runner._state() is JobState.sample_ready
    row = coordinator.store.rows(
        "SELECT status, reason, calls_sent, calls_dropped_cap FROM leads WHERE job_id='job' AND lead_id=?",
        (held_back_lead,),
    )[0]
    assert tuple(row) == ("held_back", "every call exceeds the per-lead cap", 0, len(lead_calls))
    assert not (coordinator.job_dir / "raw" / f"{held_back_lead}.jsonl.gz").exists()

    prompts: list[str] = []

    class RecordingPool(ScriptedPool):
        async def run_until_idle(self) -> None:
            while True:
                tasks = await self.callbacks.next_tasks(self.concurrency)
                if not tasks:
                    return
                for task in tasks:
                    prompts.append(task.prompt)
                    await self.callbacks.on_started(task)
                    await self.callbacks.on_result(task, {"ok": True}, task.model, None)

    updated = replace(
        coordinator.runner.spec,
        spec_version=2,
        cap=replace(coordinator.runner.spec.cap, max_chars=1000),
    )
    save_spec(updated, coordinator.job_dir)
    edit_spec(coordinator.store, "job", 2)
    coordinator.runner.on_plan_approved(2)
    monkeypatch.setattr(runner_module, "LunaPool", partial(RecordingPool, control=Script()))
    asyncio.run(asyncio.wait_for(coordinator.runner.run_sample(), timeout=15))
    coordinator.runner.on_sample_approved(2)
    asyncio.run(asyncio.wait_for(coordinator.runner.start_full_run(), timeout=15))
    expected_chars = coordinator.runner._bundle_metadata[held_back_lead][2]
    row = coordinator.store.rows(
        "SELECT status, calls_sent, calls_dropped_cap, chars FROM leads WHERE job_id='job' AND lead_id=?",
        (held_back_lead,),
    )[0]
    assert tuple(row) == ("done", 1, 0, expected_chars)
    assert any("CALL 1 of 1" in prompt for prompt in prompts)


def test_quota_pause_after_plan_edit_is_saved_as_quota_and_auto_resumes(
    coordinator: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    class PauseOncePool(ScriptedPool):
        async def run_until_idle(self) -> None:
            tasks = await self.callbacks.next_tasks(self.concurrency)
            for task in tasks[:1]:
                await self.callbacks.on_started(task)
                await self.callbacks.on_result(task, {"ok": True}, task.model, None)
                await self.callbacks.on_pause("user", None)
                return

    monkeypatch.setattr(runner_module, "LunaPool", partial(PauseOncePool, control=Script()))
    coordinator.runner.on_plan_approved(1)
    asyncio.run(asyncio.wait_for(coordinator.runner.run_sample(), timeout=15))
    assert coordinator.runner._state() is JobState.paused

    updated = replace(coordinator.runner.spec, spec_version=2, instructions="Return v2.")
    save_spec(updated, coordinator.job_dir)
    edit_spec(coordinator.store, "job", 2)
    coordinator.runner.on_plan_approved(2)
    coordinator.runner._phase_pool = None
    coordinator.pool.used_percent = 94
    coordinator.pool.quota_increment = 100
    monkeypatch.setattr(runner_module, "LunaPool", partial(DeterministicPool, control=coordinator.pool))
    asyncio.run(asyncio.wait_for(coordinator.runner.run_sample(), timeout=15))
    state, reason, resume_at = coordinator.store.rows(
        "SELECT state, pause_reason, resume_at FROM jobs WHERE id='job'"
    )[0]
    assert (state, reason) == (JobState.paused, "quota")
    assert resume_at is not None
    assert any(
        event.kind == "pause" and event.data.get("reason") == "quota" for event in coordinator.events.events
    )
    coordinator.clock.current = float(resume_at)
    coordinator.pool.used_percent = 0
    coordinator.pool.quota_increment = 0
    asyncio.run(asyncio.wait_for(coordinator.runner.resume("automatic"), timeout=15))
    assert coordinator.runner._state() is JobState.sample_ready
    assert (
        coordinator.store.rows(
            "SELECT COUNT(*) FROM results WHERE job_id='job' AND lead_id IN "
            "(SELECT lead_id FROM leads WHERE job_id='job' AND in_sample=1)"
        )[0][0]
        == 4
    )
