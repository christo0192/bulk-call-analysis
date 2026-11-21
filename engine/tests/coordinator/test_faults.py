from __future__ import annotations

import asyncio
import gzip
import json
from dataclasses import dataclass, field, replace
from functools import partial
from typing import Any

import pytest
from openpyxl import load_workbook

import bca.coordinator.runner as runner_module
from bca.coordinator.runner import _Callbacks
from bca.mask.leakscan import Hit, LeakReport, scan_lead
from bca.outputs.verify import verify_job
from bca.store import (
    Condition,
    JobState,
    LeadStatus,
    QuoteField,
    ReviewFlagRule,
    save_spec,
    start_full_run,
)
from bca.store.reconcile import reconcile
from tests.coordinator.conftest import CoordinatorFixture


def _run(awaitable: Any) -> Any:
    return asyncio.run(asyncio.wait_for(awaitable, timeout=15))


@dataclass
class Script:
    prompts: list[str] = field(default_factory=list)
    calls: int = 0
    repair_answer: dict[str, object] | None = None
    repair_paused: bool = False


class ScriptedPool:
    def __init__(
        self,
        _launch: object,
        callbacks: Any,
        _guard: object,
        concurrency: int,
        _lead_timeout: float,
        control: Script,
        **_kwargs: object,
    ) -> None:
        self.callbacks = callbacks
        self.concurrency = concurrency
        self.control: Script = control
        self.paused = False

    def set_concurrency(self, n: int) -> None:
        self.concurrency = max(1, n)

    async def stop(self) -> None:
        return

    async def pause(self, reason: str) -> None:
        del reason
        self.paused = True

    async def run_until_idle(self) -> None:
        while True:
            if self.paused:
                return
            tasks = await self.callbacks.next_tasks(self.concurrency)
            if not tasks:
                return
            for task in tasks:
                self.control.prompts.append(task.prompt)
                await self.callbacks.on_started(task)
                answer = self.control.repair_answer if task.repair else {"ok": True}
                await self.callbacks.on_result(task, answer or {"ok": True}, task.model, None)
                self.control.calls += 1


def _full_run(fixture: CoordinatorFixture) -> None:
    fixture.runner.on_plan_approved(1)
    _run(fixture.runner.run_sample())
    fixture.runner.on_sample_approved(1)
    _run(fixture.runner.start_full_run())


def test_crash_mid_run_then_recover_no_duplicates(coordinator: CoordinatorFixture, make_runner: Any) -> None:
    coordinator.runner.on_plan_approved(1)
    _run(coordinator.runner.run_sample())
    coordinator.runner.on_sample_approved(1)
    start_full_run(coordinator.store, "job", "operator", 1)
    coordinator.runner._prepare_leads(["7"], False)
    coordinator.store.set_lead_status("job", "7", LeadStatus.queued)
    assert coordinator.store.claim_queued("job", ["7"], False) == {"7"}
    attempt = coordinator.store.start_attempt("job", "7", "codex", claimed=True)

    recovered = make_runner()
    assert recovered.recover() == 1
    assert (
        coordinator.store.rows("SELECT status FROM leads WHERE job_id='job' AND lead_id='7'")[0][0]
        == "queued"
    )
    _run(recovered.start_full_run())

    assert recovered._state() is JobState.done
    assert coordinator.store.rows("SELECT lead_id FROM results WHERE job_id='job' AND lead_id='7'")
    assert coordinator.store.rows("SELECT COUNT(*) FROM results WHERE job_id='job'")[0][0] == 34
    assert (
        coordinator.store.rows("SELECT outcome FROM attempts WHERE attempt_id=?", (attempt,))[0][0]
        == "requeued"
    )


def test_recovered_lead_call_identity_reconciles(coordinator: CoordinatorFixture, make_runner: Any) -> None:
    coordinator.runner.on_plan_approved(1)
    _run(coordinator.runner.run_sample())
    coordinator.runner.on_sample_approved(1)
    start_full_run(coordinator.store, "job", "operator", 1)
    coordinator.runner._prepare_leads(["7"], False)
    coordinator.store.set_lead_status("job", "7", LeadStatus.queued)
    assert coordinator.store.claim_queued("job", ["7"], False) == {"7"}
    coordinator.store.start_attempt("job", "7", "codex", claimed=True)

    recovered = make_runner()
    assert recovered.recover() == 1
    _run(recovered.start_full_run())

    assert reconcile(coordinator.store, "job", final=True).ok


def test_sleep_gap_requeues_uncounted(coordinator: CoordinatorFixture) -> None:
    coordinator.runner.on_plan_approved(1)
    coordinator.store.set_lead_status("job", "7", LeadStatus.queued)
    coordinator.store.set_lead_status("job", "8", LeadStatus.queued)
    first = coordinator.store.start_attempt("job", "7", "codex")
    second = coordinator.store.start_attempt("job", "8", "codex")

    assert coordinator.store.requeue_in_flight("job", "sleep gap") == 2
    rows = coordinator.store.rows(
        "SELECT outcome, counted FROM attempts WHERE attempt_id IN (?, ?) ORDER BY attempt_id",
        (first, second),
    )
    assert [(str(row[0]), int(row[1])) for row in rows] == [("requeued", 0), ("requeued", 0)]
    assert (
        coordinator.store.rows("SELECT COUNT(*) FROM attempts WHERE job_id=? AND counted=1", ("job",))[0][0]
        == 0
    )


def test_pause_is_idempotent_and_late_result_is_ignored(coordinator: CoordinatorFixture) -> None:
    coordinator.runner.on_plan_approved(1)
    callbacks = _Callbacks(coordinator.runner, sample=True)
    task = _run(callbacks.next_tasks(1))[0]
    _run(callbacks.on_started(task))
    store_attempt_id = coordinator.runner._attempts[task.attempt_id]

    _run(callbacks.on_pause("sleep", None))
    _run(callbacks.on_pause("quota", None))
    _run(callbacks.on_result(task, {"ok": True}, task.model, None))

    assert coordinator.runner._state() is JobState.paused
    assert (
        coordinator.store.rows(
            "SELECT status FROM leads WHERE job_id=? AND lead_id=?", ("job", task.lead_id)
        )[0][0]
        == "queued"
    )
    assert [
        tuple(row)
        for row in coordinator.store.rows(
            "SELECT counted, outcome FROM attempts WHERE job_id=? AND attempt_id=?", ("job", store_attempt_id)
        )
    ] == [(0, "interrupted")]
    assert (
        coordinator.store.rows(
            "SELECT COUNT(*) FROM results WHERE job_id=? AND lead_id=?", ("job", task.lead_id)
        )[0][0]
        == 0
    )
    assert (
        coordinator.store.rows("SELECT COUNT(*) FROM job_events WHERE job_id='job' AND kind='stale_result'")[
            0
        ][0]
        == 0
    )


def test_repeated_task_selection_claims_each_lead_once(coordinator: CoordinatorFixture) -> None:
    coordinator.runner.on_plan_approved(1)
    coordinator.runner._prepare_leads(["7", "8"], True)
    callbacks = _Callbacks(coordinator.runner, sample=True)

    async def select_and_start() -> list[str]:
        tasks = await callbacks.next_tasks(1)
        for task in tasks:
            await callbacks.on_started(task)
        return [task.lead_id for task in tasks]

    async def select_both() -> list[list[str]]:
        return list(await asyncio.gather(select_and_start(), select_and_start()))

    selected = _run(select_both())
    lead_ids = [lead_id for batch in selected for lead_id in batch]
    assert len(lead_ids) == len(set(lead_ids))
    assert set(lead_ids) == {"10", "11"}


def test_leak_scan_holds_back_lead_and_never_sends_it(
    coordinator: CoordinatorFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator.runner.on_plan_approved(1)
    raw = coordinator.job_dir / "raw" / "0.jsonl.gz"
    with gzip.open(raw, "wt", encoding="utf-8", newline="\n") as handle:
        handle.write(
            json.dumps({"lead_id": "0", "transcript_text": "Synthetic PII 987-654-3210", "duration": 200})
            + "\n"
        )
    script = Script()
    monkeypatch.setattr(runner_module, "LunaPool", partial(ScriptedPool, control=script))
    original_scan = scan_lead

    def planted_scan(path: Any, allow_ids: Any) -> LeakReport:
        if path.name == "0.jsonl.gz":
            return LeakReport([Hit("PHONE_LIKE", 0, 12)])
        return original_scan(path, allow_ids)

    monkeypatch.setattr(runner_module, "scan_lead", planted_scan)
    coordinator.runner._prepare_leads(["0"], True)
    _run(coordinator.runner.run_sample())

    assert (
        coordinator.store.rows("SELECT status FROM leads WHERE job_id='job' AND lead_id='0'")[0][0]
        == LeadStatus.held_back
    )
    assert all("987-654-3210" not in prompt for prompt in script.prompts)


@pytest.mark.parametrize(
    "repair_answer", [{"ok": True, "quote": "says hello"}, {"ok": True, "quote": "still wrong"}]
)
def test_repair_pass(
    coordinator: CoordinatorFixture, monkeypatch: pytest.MonkeyPatch, repair_answer: dict[str, object]
) -> None:
    spec = coordinator.runner.spec
    updated = replace(
        spec,
        quote_fields=(QuoteField("quote", "any"),),
        output_schema={
            "type": "object",
            "properties": {"ok": {"type": "boolean"}, "quote": {"type": "string"}},
        },
    )
    save_spec(updated, coordinator.job_dir)
    with coordinator.store.transaction() as cur:
        cur.execute("UPDATE jobs SET spec_version=2 WHERE id='job'")
    runner = type(coordinator.runner)("job", coordinator.runner.deps)
    script = Script(repair_answer=repair_answer)
    monkeypatch.setattr(runner_module, "LunaPool", partial(ScriptedPool, control=script))
    runner.on_plan_approved(2)
    with gzip.open(
        runner.deps.job_dir / "raw" / "10.jsonl.gz", "wt", encoding="utf-8", newline="\n"
    ) as handle:
        handle.write(
            json.dumps({"lead_id": "10", "transcript_text": "Learner: says hello", "duration": 200}) + "\n"
        )
    runner._prepare_leads(["10"], True)
    _run(runner.run_sample())

    attempts = coordinator.store.rows(
        "SELECT outcome, counted FROM attempts WHERE job_id='job' AND lead_id='10'"
    )
    result = coordinator.store.rows(
        "SELECT answer_json, quotes_ok, flags_json FROM results WHERE job_id='job' AND lead_id='10'"
    )[0]
    assert len(attempts) == 2
    assert sum(int(row[1]) for row in attempts) == 2
    answer = json.loads(str(result[0]))
    if repair_answer["quote"] == "says hello":
        assert int(result[1]) == 1
    else:
        assert int(result[1]) == 0
        assert "quote:not found" in str(result[2])
    if repair_answer["quote"] == "still wrong":
        # A failed repair must preserve the original answer and its gate flag.
        assert answer == {"ok": True}
    else:
        assert answer == repair_answer


def test_forbidden_item_during_repair_keeps_answer_and_reconciles(
    coordinator: CoordinatorFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    updated = replace(coordinator.runner.spec, quote_fields=(QuoteField("quote", "any"),))
    save_spec(updated, coordinator.job_dir)
    with coordinator.store.transaction() as cur:
        cur.execute("UPDATE jobs SET spec_version=2 WHERE id='job'")
    for row in coordinator.bq.pages[0]:
        if str(row["lead_id"]) == "10":
            row["transcript_text"] = "Learner: says hello"

    class ForbiddenPool(ScriptedPool):
        async def run_until_idle(self) -> None:
            while True:
                tasks = await self.callbacks.next_tasks(self.concurrency)
                if not tasks:
                    return
                for task in tasks:
                    await self.callbacks.on_started(task)
                    if task.lead_id == "10" and not task.repair:
                        await self.callbacks.on_result(task, {"ok": True}, task.model, None)
                    elif task.lead_id in {"10", "11"}:
                        await self.callbacks.on_failure(
                            task, "tool_call_forbidden", True, "synthetic forbidden"
                        )
                    else:
                        await self.callbacks.on_result(task, {"ok": True, "quote": "hello"}, task.model, None)

    monkeypatch.setattr(runner_module, "LunaPool", partial(ForbiddenPool, control=Script()))
    coordinator.runner.on_plan_approved(2)
    _run(coordinator.runner.run_sample())
    coordinator.runner.on_sample_approved(2)
    _run(coordinator.runner.start_full_run())

    lead_10 = coordinator.store.rows(
        "SELECT status, reason, answer_json FROM leads "
        "LEFT JOIN results USING (job_id, lead_id) WHERE leads.job_id='job' AND leads.lead_id='10'"
    )[0]
    assert tuple(lead_10[:2]) == ("done", "repair blocked: tool_call_forbidden")
    assert json.loads(str(lead_10[2])) == {"ok": True}
    assert reconcile(coordinator.store, "job", final=True).ok
    # The workbook is named after the spec version (2 here).
    assert verify_job(coordinator.store, "job", coordinator.job_dir / "output" / "results.v2.xlsx").ok

    lead_11_attempts = coordinator.store.rows(
        "SELECT status, reason, COUNT(*) FROM leads "
        "LEFT JOIN attempts USING (job_id, lead_id) "
        "WHERE leads.job_id='job' AND leads.lead_id='11' GROUP BY status, reason"
    )[0]
    assert tuple(lead_11_attempts) == ("failed", "tool_call_forbidden", 1)


def test_retry_exhaustion_ends_done_with_issues(coordinator: CoordinatorFixture) -> None:
    coordinator.pool.actions["7"] = "fail"
    _full_run(coordinator)

    assert coordinator.runner._state() is JobState.done_with_issues
    assert (
        coordinator.store.rows("SELECT status FROM leads WHERE job_id='job' AND lead_id='7'")[0][0]
        == LeadStatus.failed
    )
    assert (
        coordinator.store.rows(
            "SELECT COUNT(*) FROM attempts WHERE job_id='job' AND lead_id='7' AND counted=1"
        )[0][0]
        == 2
    )


def test_rebuild_uses_no_model_and_changes_only_rule_columns(coordinator: CoordinatorFixture) -> None:
    _full_run(coordinator)
    before = coordinator.pool.calls
    source = coordinator.job_dir / "output" / "results.v1.xlsx"
    rule = ReviewFlagRule("review", "all", (Condition("ok", "eq", True),))
    rebuilt = coordinator.runner.rebuild_with_rules((rule,))
    assert coordinator.pool.calls == before
    assert rebuilt.name == "results.v1.rebuild1.xlsx"
    assert (
        coordinator.store.rows("SELECT COUNT(*) FROM job_events WHERE job_id='job' AND kind='rules_rebuilt'")[
            0
        ][0]
        == 1
    )
    second = coordinator.runner.rebuild_with_rules(())
    assert second.name == "results.v1.rebuild2.xlsx"
    assert (
        coordinator.store.rows("SELECT COUNT(*) FROM job_events WHERE job_id='job' AND kind='rules_rebuilt'")[
            0
        ][0]
        == 2
    )

    old = load_workbook(source, read_only=True, data_only=True)
    new = load_workbook(rebuilt, read_only=True, data_only=True)
    try:
        old_rows, new_rows = list(old["Results"].values), list(new["Results"].values)
    finally:
        old.close()
        new.close()
    old_headers, new_headers = list(old_rows[0]), list(new_rows[0])
    for key in ("lead id", "ok", "quote checks ok"):
        assert [row[old_headers.index(key)] for row in old_rows[1:]] == [
            row[new_headers.index(key)] for row in new_rows[1:]
        ]
    assert "review" in new_headers


def test_bus_events_include_progress_eta_liveness(coordinator: CoordinatorFixture) -> None:
    coordinator.runner.on_plan_approved(1)
    _run(coordinator.runner.run_sample())
    kinds = {event.kind for event in coordinator.events.events}
    assert {"counts", "eta"}.issubset(kinds)
    assert all(event.data.get("liveness") != "active" for event in coordinator.events.events)


def test_activity_persists_stall_before_refreshing_liveness(coordinator: CoordinatorFixture) -> None:
    coordinator.runner._liveness.pool_silence_s = 1
    for lead_id in ("synthetic-1", "synthetic-2", "synthetic-3"):
        coordinator.runner._liveness.lead_started(lead_id, coordinator.clock.now())
    coordinator.clock.advance_wall(2)
    callbacks = _Callbacks(coordinator.runner)

    _run(callbacks.on_activity(coordinator.clock.now()))

    assert any(event.kind == "stall" for event in coordinator.events.events)
