from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest

from bca.mcp.host import ToolHostError
from bca.store import JobState, LeadStatus, edit_spec, save_spec
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
    host = proposal_host(tmp_path, [[]])
    host.call("propose_job", {"spec_json": proposal_json()})
    with host.store.transaction() as cur:
        cur.execute("UPDATE jobs SET state='sample_running' WHERE id='job'")
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


def test_over_cap_hold_back_is_reevaluated_after_cap_raise(coordinator: Any) -> None:
    updated = replace(
        coordinator.runner.spec,
        spec_version=2,
        cap=replace(coordinator.runner.spec.cap, max_chars=1000),
    )
    save_spec(updated, coordinator.job_dir)
    edit_spec(coordinator.store, "job", 2)
    coordinator.runner.on_plan_approved(2)
    asyncio.run(asyncio.wait_for(coordinator.runner.run_sample(), timeout=15))
    row = coordinator.store.rows(
        "SELECT status, calls_sent, calls_dropped_cap FROM leads WHERE job_id='job' AND lead_id='6'"
    )[0]
    assert tuple(row) == (LeadStatus.done, 2, 0)
    assert not (coordinator.job_dir / "raw" / "6.jsonl.gz").exists()


def test_quota_pause_after_plan_edit_is_saved_as_quota_and_auto_resumes(coordinator: Any) -> None:
    updated = replace(coordinator.runner.spec, spec_version=2, instructions="Return v2.")
    save_spec(updated, coordinator.job_dir)
    edit_spec(coordinator.store, "job", 2)
    coordinator.runner.on_plan_approved(2)
    coordinator.pool.quota_increment = 100
    asyncio.run(asyncio.wait_for(coordinator.runner.run_sample(), timeout=15))
    state, reason, resume_at = coordinator.store.rows(
        "SELECT state, pause_reason, resume_at FROM jobs WHERE id='job'"
    )[0]
    assert (state, reason) == (JobState.paused, "quota")
    assert resume_at is not None
    assert any(event.kind == "pause" for event in coordinator.events.events)
    coordinator.clock.current = float(resume_at)
    coordinator.pool.quota_increment = 0
    asyncio.run(asyncio.wait_for(coordinator.runner.resume("automatic"), timeout=15))
    assert coordinator.runner._state() is JobState.sample_ready
