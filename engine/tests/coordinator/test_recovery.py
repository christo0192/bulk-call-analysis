from __future__ import annotations

import asyncio
import gzip
import json
import threading
from dataclasses import replace
from functools import partial
from typing import Any

import pytest

import bca.coordinator.runner as runner_module
from bca.mask.masker import mask_lead_stream
from bca.store import JobState, LeadStatus, edit_spec, save_spec
from tests.coordinator.conftest import CoordinatorFixture
from tests.coordinator.test_faults import Script, ScriptedPool


def _run(awaitable: Any) -> Any:
    return asyncio.run(asyncio.wait_for(awaitable, timeout=15))


class _ProcessDeath(BaseException):
    """Simulates the engine process dying: not caught by `except Exception`."""


def test_fresh_runner_full_run_uses_persisted_harvest(
    coordinator: CoordinatorFixture, make_runner: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
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

    monkeypatch.setattr(runner_module, "LunaPool", partial(RecordingPool, control=Script()))
    coordinator.runner.on_plan_approved(1)
    _run(coordinator.runner.run_sample())
    coordinator.runner.on_sample_approved(1)
    fresh = make_runner()
    _run(fresh.start_full_run())
    assert prompts
    assert all("Synthetic Name" not in prompt for prompt in prompts)


def test_missing_harvest_file_fails_closed(coordinator: CoordinatorFixture) -> None:
    coordinator.runner.on_plan_approved(1)
    _run(coordinator.runner.run_sample())
    coordinator.runner.on_sample_approved(1)
    (coordinator.job_dir / "harvested_names.json").unlink()
    _run(coordinator.runner.start_full_run())
    assert (
        coordinator.store.rows("SELECT COUNT(*) FROM leads WHERE job_id='job' AND status='held_back'")[0][0]
        > 0
    )


def test_empty_harvest_is_valid(coordinator: CoordinatorFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_module, "harvest_seen_twice", lambda *_: [])
    coordinator.runner.on_plan_approved(1)
    _run(coordinator.runner.run_sample())
    assert coordinator.runner._state() is JobState.sample_ready


def test_recover_after_crash_during_pull(
    coordinator: CoordinatorFixture, make_runner: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_pull = runner_module.pull_transcripts  # type: ignore[attr-defined]
    crashed = False

    def crash_once(*args: Any, **kwargs: Any) -> Any:
        nonlocal crashed
        if not crashed:
            crashed = True
            raise _ProcessDeath("synthetic pull crash")
        return original_pull(*args, **kwargs)

    monkeypatch.setattr(runner_module, "pull_transcripts", crash_once)
    with pytest.raises(_ProcessDeath, match="synthetic pull crash"):
        coordinator.runner.on_plan_approved(1)

    recovered = make_runner()
    recovered.recover()
    asyncio.run(asyncio.wait_for(recovered.run_sample(), timeout=15))
    assert recovered._state() is JobState.sample_ready
    assert coordinator.store.rows("SELECT COUNT(*) FROM results WHERE job_id='job'")[0][0] == 4


def test_recover_after_crash_masking_second_sample_lead(
    coordinator: CoordinatorFixture, make_runner: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The crash happens INSIDE masking_sample: the leak scan of the 2nd sample lead kills the
    # process during on_plan_approved, after the 1st sample lead was masked and queued.
    original_scan = runner_module.scan_lead
    scans = {"n": 0}

    def dying_scan(*args: Any, **kwargs: Any) -> Any:
        scans["n"] += 1
        if scans["n"] == 2:
            raise _ProcessDeath()
        return original_scan(*args, **kwargs)

    monkeypatch.setattr(runner_module, "scan_lead", dying_scan)
    with pytest.raises(_ProcessDeath):
        coordinator.runner.on_plan_approved(1)
    monkeypatch.setattr(runner_module, "scan_lead", original_scan)
    assert coordinator.runner._state() is JobState.masking_sample
    assert scans["n"] == 2

    recovered = make_runner()
    recovered.recover()
    asyncio.run(asyncio.wait_for(recovered.run_sample(), timeout=15))
    assert recovered._state() is JobState.sample_ready
    assert (
        coordinator.store.rows(
            "SELECT COUNT(*) FROM results WHERE job_id='job' AND lead_id IN ('10', '11', '12', '13')"
        )[0][0]
        == 4
    )
    assert (
        coordinator.store.rows(
            "SELECT COUNT(*) FROM leads WHERE job_id='job' AND in_sample=1 AND status='done'"
        )[0][0]
        == 4
    )
    assert not any(
        (coordinator.job_dir / "raw" / f"{lead_id}.jsonl.gz").exists() for lead_id in ("10", "11", "12", "13")
    )


def test_harvest_receives_a_streaming_iterator(
    coordinator: CoordinatorFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    received: list[object] = []

    def harvesting(calls: Any, _ner: Any) -> list[str]:
        received.append(calls)
        assert not isinstance(calls, list)
        assert sum(1 for _ in calls) > 0
        return []

    monkeypatch.setattr(runner_module, "harvest_seen_twice", harvesting)
    coordinator.runner.on_plan_approved(1)

    assert received
    assert not (coordinator.job_dir / "raw" / "3.jsonl.gz").exists()


@pytest.mark.parametrize("phase", ["sample_running", "full_running", "quote_check", "building_output"])
def test_recover_after_crash_in_each_phase(
    coordinator: CoordinatorFixture, make_runner: Any, phase: str
) -> None:
    coordinator.runner.on_plan_approved(1)
    if phase == "sample_running":
        recovered = make_runner()
        recovered.recover()
        _run(recovered.run_sample())
        assert recovered._state() is JobState.sample_ready
        recovered.on_sample_approved(1)
        _run(recovered.start_full_run())
    else:
        _run(coordinator.runner.run_sample())
        coordinator.runner.on_sample_approved(1)
        if phase == "full_running":
            recovered = make_runner()
            recovered.recover()
            _run(recovered.start_full_run())
        else:
            _run(coordinator.runner.start_full_run())
            with coordinator.store.transaction() as cur:
                cur.execute("UPDATE jobs SET state=? WHERE id='job'", (phase,))
            recovered = make_runner()
            recovered.recover()
            _run(recovered.start_full_run())
    assert recovered._state() in {JobState.done, JobState.done_with_issues}
    assert coordinator.store.rows("SELECT COUNT(*) FROM results WHERE job_id='job'")[0][0] == 34
    assert (
        coordinator.store.rows("SELECT COUNT(*) FROM leads WHERE job_id='job' AND status='in_flight'")[0][0]
        == 0
    )


@pytest.mark.timeout(5)
def test_recover_fresh_runner_when_full_run_has_no_masking_left(
    coordinator: CoordinatorFixture, make_runner: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator.runner.on_plan_approved(1)
    _run(coordinator.runner.run_sample())
    coordinator.runner.on_sample_approved(1)

    original_on_result = runner_module._Callbacks.on_result
    crashed = False

    async def crash_on_first_full_result(callbacks: Any, task: Any, *args: Any, **kwargs: Any) -> None:
        nonlocal crashed
        if not callbacks.sample and not crashed:
            crashed = True
            raise RuntimeError("synthetic runner crash")
        await original_on_result(callbacks, task, *args, **kwargs)

    monkeypatch.setattr(runner_module._Callbacks, "on_result", crash_on_first_full_result)
    with pytest.raises(RuntimeError, match="synthetic runner crash"):
        _run(coordinator.runner.start_full_run())

    # The crash fires once; later calls pass through to the real on_result. Do not
    # monkeypatch.undo() here: that would also undo the fixture's deterministic pool.
    recovered = make_runner()
    recovered.recover()
    _run(recovered.start_full_run())

    assert recovered._state() in {JobState.done, JobState.done_with_issues}
    usable_leads = {
        str(lead_id)
        for (lead_id,) in coordinator.store.rows(
            "SELECT lead_id FROM leads WHERE job_id='job' AND status='done'"
        )
    }
    outcomes = coordinator.store.rows(
        "SELECT lead_id, COUNT(*) FROM attempts WHERE job_id='job' AND counted=1 GROUP BY lead_id"
    )
    assert {str(lead_id) for lead_id, _count in outcomes} == usable_leads
    assert all(int(count) == 1 for _lead_id, count in outcomes)


def test_resume_on_fresh_runner_after_pause(coordinator: CoordinatorFixture, make_runner: Any) -> None:
    coordinator.runner.on_plan_approved(1)
    coordinator.store.set_lead_status("job", "0", LeadStatus.queued)
    coordinator.store.start_attempt("job", "0", "codex")
    from bca.store import PauseReason, pause

    pause(coordinator.store, "job", PauseReason.user)
    recovered = make_runner()
    with pytest.raises(PermissionError):
        _run(recovered.resume("automatic"))
    _run(recovered.resume("operator"))
    assert recovered._state() is JobState.sample_ready


def test_plan_edit_then_reapprove_uses_new_spec(
    coordinator: CoordinatorFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = replace(coordinator.runner.spec, instructions="Return v2.")
    save_spec(spec, coordinator.job_dir)
    with coordinator.store.transaction() as cur:
        cur.execute("UPDATE jobs SET spec_version=2 WHERE id='job'")
    control = Script()
    monkeypatch.setattr(runner_module, "LunaPool", partial(ScriptedPool, control=control))
    coordinator.runner.on_plan_approved(2)
    _run(coordinator.runner.run_sample())
    assert control.prompts and all("Return v2." in prompt for prompt in control.prompts)
    assert (
        coordinator.store.rows("SELECT COUNT(*) FROM leads WHERE job_id='job' AND status='in_flight'")[0][0]
        == 0
    )


def test_cap_edit_after_background_masking_rebundles_full_run_leads(
    coordinator: CoordinatorFixture, make_runner: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Lead 6 has two calls; v1's cap keeps only its long call. Materialize that
    # bundle before the edit to exercise the spec-version cache invalidation.
    coordinator.runner.on_plan_approved(1)
    _run(coordinator.runner.run_sample())
    assert coordinator.runner._bundles["6"][0] == 1
    with gzip.open(coordinator.job_dir / "masked" / "6.jsonl.gz", "rt", encoding="utf-8") as source:
        call_chars = [len(str(json.loads(line)["transcript_text"])) for line in source]
    # Both masked calls are stored; the v1 cap sends only the long one.
    assert len(call_chars) == 2
    assert coordinator.runner._bundle_metadata["6"] == (1, 1, max(call_chars))
    assert tuple(
        coordinator.store.rows(
            "SELECT calls_sent, calls_dropped_cap FROM leads WHERE job_id='job' AND lead_id='6'"
        )[0]
    ) == (1, 1)
    coordinator.runner.on_sample_approved(1)

    updated = replace(
        coordinator.runner.spec,
        spec_version=2,
        cap=replace(coordinator.runner.spec.cap, max_chars=1000),
    )
    save_spec(updated, coordinator.job_dir)
    edit_spec(coordinator.store, "job", 2)
    coordinator.runner.on_plan_approved(2)
    _run(coordinator.runner.run_sample())
    coordinator.runner.on_sample_approved(2)

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

    monkeypatch.setattr(runner_module, "LunaPool", partial(RecordingPool, control=Script()))
    # The edited plan requires a fresh sample approval; make the already
    # materialized lead part of the full-run set for this focused regression.
    with coordinator.store.transaction() as cur:
        cur.execute("UPDATE leads SET in_sample=0, status='queued' WHERE job_id='job' AND lead_id='6'")
    fresh = make_runner()
    _run(fresh.start_full_run())

    assert any("CALL 2 of 2" in prompt for prompt in prompts)
    assert tuple(
        coordinator.store.rows(
            "SELECT calls_sent, calls_dropped_cap, chars FROM leads WHERE job_id='job' AND lead_id='6'"
        )[0]
    ) == (2, 0, sum(call_chars))


def test_lead_with_all_calls_over_cap_is_held_back_not_sent(
    coordinator: CoordinatorFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    held_back_lead = "20"
    lead_calls = [call for call in coordinator.bq.pages[0] if str(call["lead_id"]) == held_back_lead]
    assert lead_calls
    for call in lead_calls:
        call["transcript_text"] = "Synthetic Name says hello. " + ("x" * 500)

    updated_spec = replace(coordinator.runner.spec, spec_version=2)
    save_spec(updated_spec, coordinator.job_dir)
    edit_spec(coordinator.store, "job", 2)

    recorded: list[tuple[str, str]] = []

    class RecordingPool(ScriptedPool):
        async def run_until_idle(self) -> None:
            while True:
                tasks = await self.callbacks.next_tasks(self.concurrency)
                if not tasks:
                    return
                for task in tasks:
                    recorded.append((task.lead_id, task.prompt))
                    await self.callbacks.on_started(task)
                    await self.callbacks.on_result(task, {"ok": True}, task.model, None)

    monkeypatch.setattr(runner_module, "LunaPool", partial(RecordingPool, control=Script()))
    coordinator.runner.on_plan_approved(2)
    _run(coordinator.runner.run_sample())
    coordinator.runner.on_sample_approved(2)
    _run(coordinator.runner.start_full_run())

    row = coordinator.store.rows(
        "SELECT status, reason, calls_sent, calls_dropped_cap FROM leads WHERE job_id='job' AND lead_id=?",
        (held_back_lead,),
    )[0]
    assert tuple(row) == (
        "held_back",
        "every call exceeds the per-lead cap",
        0,
        len(lead_calls),
    )
    assert held_back_lead not in {lead_id for lead_id, _prompt in recorded}
    assert not [prompt for lead_id, prompt in recorded if lead_id == held_back_lead]

    preflight_pages = coordinator.bq.preflight_pages
    assert preflight_pages is not None
    eligible_leads = {str(row["lead_id"]) for row in preflight_pages[0] if int(str(row["usable_calls"])) > 0}
    statuses = coordinator.store.rows(
        "SELECT lead_id, status FROM leads WHERE job_id='job' AND lead_id IN "
        "(SELECT lead_id FROM leads WHERE job_id='job' AND status != 'held_back')"
    )
    assert {
        str(lead_id): str(status)
        for lead_id, status in statuses
        if str(lead_id) in eligible_leads - {held_back_lead}
    } == {lead_id: "done" for lead_id in eligible_leads - {held_back_lead}}
    assert coordinator.runner._state() in {JobState.done, JobState.done_with_issues}


def test_repair_attempt_counted_failure_keeps_answer_with_flag(
    coordinator: CoordinatorFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bca.store import QuoteField

    spec = replace(coordinator.runner.spec, quote_fields=(QuoteField("quote", "any"),))
    save_spec(spec, coordinator.job_dir)
    with coordinator.store.transaction() as cur:
        cur.execute("UPDATE jobs SET spec_version=2 WHERE id='job'")

    class FailingRepairPool(ScriptedPool):
        async def run_until_idle(self) -> None:
            tasks = await self.callbacks.next_tasks(self.concurrency)
            for task in tasks:
                await self.callbacks.on_started(task)
                if task.repair:
                    await self.callbacks.on_failure(task, "invalid_output", True, "synthetic")
                else:
                    await self.callbacks.on_result(task, {"ok": True}, task.model, None)

    monkeypatch.setattr(runner_module, "LunaPool", partial(FailingRepairPool, control=Script()))
    coordinator.runner.on_plan_approved(2)
    _run(coordinator.runner.run_sample())
    assert coordinator.runner._state() is JobState.sample_ready
    assert (
        coordinator.store.rows(
            "SELECT COUNT(*) FROM leads WHERE job_id='job' AND in_sample=1 "
            "AND status IN ('queued','in_flight')"
        )[0][0]
        == 0
    )


def test_pause_during_repair_sends_repair_prompt_on_resume(
    coordinator: CoordinatorFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompts: list[str] = []

    class PauseRepairPool(ScriptedPool):
        async def run_until_idle(self) -> None:
            tasks = await self.callbacks.next_tasks(self.concurrency)
            for task in tasks:
                prompts.append(task.prompt)
                await self.callbacks.on_started(task)
                if not task.repair:
                    await self.callbacks.on_result(task, {"ok": False}, task.model, None)
                else:
                    if not self.control.repair_paused:
                        self.control.repair_paused = True
                        await self.callbacks.on_pause("user", None)
                        return
                    await self.callbacks.on_result(task, {"ok": False, "quote": "repaired"}, task.model, None)

    monkeypatch.setattr(runner_module, "LunaPool", partial(PauseRepairPool, control=Script()))
    from bca.store import QuoteField

    updated = replace(coordinator.runner.spec, quote_fields=(QuoteField("quote", "any"),))
    save_spec(updated, coordinator.job_dir)
    with coordinator.store.transaction() as cur:
        cur.execute("UPDATE jobs SET spec_version=2 WHERE id='job'")
    coordinator.runner.on_plan_approved(2)
    _run(coordinator.runner.run_sample())
    assert coordinator.runner._state() is JobState.paused
    with pytest.raises(PermissionError):
        _run(coordinator.runner.resume("automatic"))
    _run(coordinator.runner.resume("operator"))
    assert coordinator.runner._state() is JobState.sample_ready
    counted_by_lead = [
        (str(lead_id), int(count))
        for lead_id, count in coordinator.store.rows(
            "SELECT lead_id, COUNT(*) FROM attempts WHERE job_id='job' AND counted=1 GROUP BY lead_id"
        )
    ]
    assert counted_by_lead
    assert all(count == 2 for _lead_id, count in counted_by_lead)
    uncounted = coordinator.store.rows(
        "SELECT lead_id, counted, error_code FROM attempts WHERE job_id='job' AND counted=0 ORDER BY lead_id"
    )
    assert len(uncounted) == 1
    assert [(str(lead_id), int(counted), str(error_code)) for lead_id, counted, error_code in uncounted] == [
        ("10", 0, "quote_repair_pending")
    ]
    assert prompts and "Repair only the failed quote fields." in prompts[-1]
    attempts = coordinator.store.rows(
        "SELECT lead_id, counted, outcome FROM attempts WHERE job_id='job' ORDER BY lead_id, started_at"
    )
    attempts_by_lead: dict[str, list[tuple[int, str | None]]] = {}
    for lead_id, counted, outcome in attempts:
        attempts_by_lead.setdefault(str(lead_id), []).append((int(counted), outcome))
    assert set(attempts_by_lead) == {str(lead_id) for lead_id, _count in counted_by_lead}
    assert attempts_by_lead["10"] == [(1, "ok"), (0, "interrupted"), (1, "ok")]
    assert all(rows == [(1, "ok"), (1, "ok")] for lead, rows in attempts_by_lead.items() if lead != "10")


def test_corrupt_raw_file_holds_back_only_that_lead(
    coordinator: CoordinatorFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompts: list[str] = []
    coordinator.runner.on_plan_approved(1)
    _run(coordinator.runner.run_sample())
    coordinator.runner.on_sample_approved(1)
    raw = coordinator.job_dir / "raw" / "20.jsonl.gz"
    raw.write_text("not gzip", encoding="utf-8")
    coordinator.store.set_lead_status("job", "20", LeadStatus.not_started)
    monkeypatch.setattr(runner_module, "LunaPool", partial(ScriptedPool, control=Script(prompts=prompts)))
    _run(coordinator.runner.start_full_run())
    assert (
        coordinator.store.rows("SELECT status FROM leads WHERE job_id='job' AND lead_id='20'")[0][0]
        == LeadStatus.held_back
    )


def test_full_run_dispatches_while_background_masking_continues(
    coordinator: CoordinatorFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = threading.Event()
    release = threading.Event()
    dispatched_during_masking: list[str] = []
    original_mask = mask_lead_stream

    def blocking_mask(raw: Any, masked: Any, context: Any) -> Any:
        if raw.name == "8.jsonl.gz":
            entered.set()
            assert release.wait(15)
        return original_mask(raw, masked, context)

    class ObservingPool(ScriptedPool):
        async def run_until_idle(self) -> None:
            while True:
                tasks = await self.callbacks.next_tasks(self.concurrency)
                if not tasks:
                    return
                for task in tasks:
                    if entered.is_set() and not release.is_set():
                        dispatched_during_masking.append(task.lead_id)
                        release.set()
                    await self.callbacks.on_started(task)
                    await self.callbacks.on_result(task, {"ok": True}, task.model, None)

    monkeypatch.setattr(runner_module, "mask_lead_stream", blocking_mask)
    monkeypatch.setattr(runner_module, "LunaPool", partial(ObservingPool, control=Script()))
    # Each _run() is a separate event loop in this test, and asyncio.run waits for its
    # worker threads on exit. Keep full-scope masking out of the sample loop so it runs
    # (and blocks on lead 8) inside start_full_run, which is the window this test checks.
    original_start_masking = coordinator.runner._start_masking_worker
    monkeypatch.setattr(coordinator.runner, "_start_masking_worker", lambda sample: None)
    coordinator.runner.on_plan_approved(1)
    _run(coordinator.runner.run_sample())
    coordinator.runner.on_sample_approved(1)
    monkeypatch.setattr(coordinator.runner, "_start_masking_worker", original_start_masking)
    _run(asyncio.wait_for(coordinator.runner.start_full_run(), timeout=30))

    assert dispatched_during_masking
    assert (
        coordinator.store.rows("SELECT status FROM leads WHERE job_id='job' AND lead_id='8'")[0][0]
        == LeadStatus.done
    )
