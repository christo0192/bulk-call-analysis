from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from openpyxl import load_workbook

import bca.coordinator.runner as runner_module
from bca.luna.appserver import AppServer, CodexLaunch, start
from bca.luna.pool import LunaPool
from bca.store import JobState, LeadSourceIds, LeadStatus, QuoteField, edit_spec, save_spec
from bca.store import start_full_run as start_full_run_job
from tests.coordinator.conftest import CoordinatorFixture
from tests.fakes import fake_codex


def _run(awaitable: Any) -> Any:
    return asyncio.run(asyncio.wait_for(awaitable, timeout=15))


def _real_pool(
    coordinator: CoordinatorFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: dict[str, object],
) -> Path:
    scenario_path = tmp_path / "fake-codex-scenario.json"
    scenario_path.write_text(json.dumps(scenario), encoding="utf-8", newline="\n")

    async def launch() -> AppServer:
        return await start(
            CodexLaunch(
                (sys.executable, str(Path(fake_codex.__file__)), "--scenario", str(scenario_path)),
                tmp_path / "codex-home",
                tmp_path,
            )
        )

    monkeypatch.setattr(runner_module, "LunaPool", LunaPool)
    coordinator.runner.deps = replace(coordinator.runner.deps, launch_pool=launch, retry_backoff_s=0.01)
    return scenario_path


def _approve_plan(coordinator: CoordinatorFixture) -> None:
    coordinator.runner.on_plan_approved(1)


def test_uncounted_failures_are_bounded_then_pause(
    coordinator: CoordinatorFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _real_pool(coordinator, tmp_path, monkeypatch, {"errors": {"10": "rateLimitExceeded"}})
    _approve_plan(coordinator)

    _run(coordinator.runner.run_sample())

    assert coordinator.runner._state() is JobState.paused
    assert coordinator.store.rows("SELECT pause_reason FROM jobs WHERE id='job'")[0][0] == "network"
    assert (
        coordinator.store.rows(
            "SELECT COUNT(*) FROM attempts WHERE job_id='job' AND lead_id='10' AND counted=0"
        )[0][0]
        == 4
    )


def test_zero_resolved_leads_stalls_with_reason(coordinator: CoordinatorFixture) -> None:
    source = cast(LeadSourceIds, coordinator.runner.spec.lead_source)
    spec = replace(
        coordinator.runner.spec,
        lead_source=replace(source, count=0),
    )
    # save_spec always writes the next version file, so the job must move to it.
    save_spec(spec, coordinator.job_dir)
    edit_spec(coordinator.store, "job", 2)
    (coordinator.job_dir / "ids.txt").write_text("\n", encoding="utf-8", newline="\n")

    coordinator.runner.on_plan_approved(2)

    assert coordinator.runner._state() is JobState.stalled
    # The stall reason is recorded on the state-transition event.
    last_state = coordinator.store.rows(
        "SELECT data_json FROM job_events WHERE job_id='job' AND kind='state' ORDER BY id DESC LIMIT 1"
    )[0][0]
    event = json.loads(str(last_state))
    assert (event["to"], event["reason"]) == ("stalled", "no leads resolved from the lead source")
    assert not (coordinator.job_dir / "output" / "results.v1.xlsx").exists()


def test_repair_always_failing_uncounted_pauses_network_after_four(
    coordinator: CoordinatorFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Raise the cap: the prefix below makes every call longer than the fixture's v1 cap.
    spec = replace(
        coordinator.runner.spec,
        quote_fields=(QuoteField("quote", "any"),),
        cap=replace(coordinator.runner.spec.cap, max_chars=1000),
    )
    save_spec(spec, coordinator.job_dir)
    edit_spec(coordinator.store, "job", 2)
    for call in coordinator.bq.pages[0]:
        call["transcript_text"] = "Customer: hello. " + str(call["transcript_text"])
    # Lead 20 is a full-run lead, so the whole scenario can be in place from the start:
    # its first answer fails the quote check, and every repair turn then fails uncounted.
    scenario_path = _real_pool(
        coordinator,
        tmp_path,
        monkeypatch,
        {
            "answers": {"20": {"quote": "not in the transcript"}},
            "default_answer": {"quote": "hello"},
            "errors_after_first_answer": {"20": "httpConnectionFailed"},
        },
    )
    coordinator.runner.on_plan_approved(2)
    _run(coordinator.runner.run_sample())
    assert (
        coordinator.store.rows("SELECT in_sample FROM leads WHERE job_id='job' AND lead_id='20'")[0][0] == 0
    )
    coordinator.runner.on_sample_approved(2)
    _run(coordinator.runner.start_full_run())

    assert coordinator.runner._state() is JobState.paused
    assert coordinator.store.rows("SELECT pause_reason FROM jobs WHERE id='job'")[0][0] == "network"
    assert (
        coordinator.store.rows("SELECT reason FROM leads WHERE job_id='job' AND lead_id='20'")[0][0]
        == "quote_repair_pending"
    )
    assert (
        coordinator.store.rows(
            "SELECT COUNT(*) FROM attempts WHERE job_id='job' AND lead_id='20' AND counted=0 "
            "AND error_code='httpConnectionFailed'"
        )[0][0]
        == 4
    )
    assert (
        coordinator.store.rows(
            "SELECT quotes_ok, flags_json, answer_json FROM results WHERE job_id='job' AND lead_id='20'"
        )[0][0]
        == 0
    )
    result = coordinator.store.rows(
        "SELECT quotes_ok, flags_json, answer_json FROM results WHERE job_id='job' AND lead_id='20'"
    )[0]
    assert "quote:" in str(result[1])
    assert "not in the transcript" in str(result[2])
    assert (
        coordinator.store.rows(
            "SELECT COUNT(*) FROM attempts WHERE job_id='job' AND lead_id='20' "
            "AND error_code IN ('interrupted', 'quota_interrupt')"
        )[0][0]
        == 0
    )

    scenario_path.write_text(
        json.dumps({"answers": {"20": {"quote": "hello"}}, "default_answer": {"quote": "hello"}}),
        encoding="utf-8",
        newline="\n",
    )
    _run(coordinator.runner.resume("operator"))
    assert coordinator.runner._state() is JobState.done
    # Lead 20's repair now succeeds; a sqlite3.Row never equals a tuple, so compare tuple(row).
    lead_row = coordinator.store.rows("SELECT status, reason FROM leads WHERE job_id='job' AND lead_id='20'")[
        0
    ]
    assert tuple(lead_row) == ("done", None)
    repaired = coordinator.store.rows("SELECT quotes_ok FROM results WHERE job_id='job' AND lead_id='20'")[0][
        0
    ]
    assert repaired == 1


def test_credit_drop_with_real_pool_pauses_and_fresh_runner_refuses_automatic_resume(
    coordinator: CoordinatorFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_runner: Any
) -> None:
    _real_pool(coordinator, tmp_path, monkeypatch, {"credit_drop": True})
    _approve_plan(coordinator)

    _run(coordinator.runner.run_sample())

    assert coordinator.runner._state() is JobState.paused
    assert coordinator.store.rows("SELECT pause_reason FROM jobs WHERE id='job'")[0][0] == "credit"
    with pytest.raises(PermissionError):
        _run(make_runner().resume("automatic"))


def test_quota_resume_waits_resume_at_then_canary_then_full_concurrency(
    coordinator: CoordinatorFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_runner: Any
) -> None:
    scenario_path = _real_pool(
        coordinator,
        tmp_path,
        monkeypatch,
        {"used_percent": 94, "quota_increment": 1, "latency_ms": 1},
    )
    _approve_plan(coordinator)
    _run(coordinator.runner.run_sample())
    assert coordinator.runner._state() is JobState.paused
    resume_at = int(coordinator.store.rows("SELECT resume_at FROM jobs WHERE id='job'")[0][0])

    fresh = make_runner()
    assert fresh.deps.clock is not None
    _run(fresh.resume("automatic"))
    assert fresh._state() is JobState.paused
    coordinator.clock.current = float(resume_at)
    scenario_path.write_text(json.dumps({"latency_ms": 1}), encoding="utf-8", newline="\n")
    _run(fresh.resume("automatic"))
    assert fresh._state() is JobState.sample_ready
    fresh.on_sample_approved(1)
    _run(fresh.start_full_run())
    assert fresh._state() is JobState.done
    assert (
        coordinator.store.rows("SELECT COUNT(*) FROM leads WHERE job_id='job' AND status='done'")[0][0] == 34
    )
    assert (
        coordinator.store.rows(
            "SELECT COUNT(*) FROM leads WHERE job_id='job' AND status IN ('queued', 'in_flight')"
        )[0][0]
        == 0
    )


def test_output_is_refused_while_a_lead_is_queued(coordinator: CoordinatorFixture) -> None:
    coordinator.runner.on_plan_approved(1)
    _run(coordinator.runner.run_sample())
    coordinator.runner.on_sample_approved(1)
    start_full_run_job(coordinator.store, "job", "operator", 1)
    coordinator.store.set_lead_status("job", "7", LeadStatus.queued)

    with pytest.raises(RuntimeError, match="leads are pending"):
        _run(coordinator.runner._finish_output(JobState.quote_check))

    assert coordinator.runner._state() is JobState.full_running
    assert not (coordinator.job_dir / "output" / "results.v1.xlsx").exists()


def test_reroute_is_recorded_and_excel_model_used_is_filled(
    coordinator: CoordinatorFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _real_pool(
        coordinator,
        tmp_path,
        monkeypatch,
        {"reroute": "10", "reroute_once": True, "models": ["codex", "codex-mini"]},
    )
    _approve_plan(coordinator)

    _run(coordinator.runner.run_sample())

    assert coordinator.runner._state() is JobState.paused
    assert coordinator.store.rows("SELECT pause_reason FROM jobs WHERE id='job'")[0][0] == "model_unavailable"
    attempt = coordinator.store.rows(
        "SELECT model_used, rerouted_from FROM attempts WHERE job_id='job' AND lead_id='10'"
    )[0]
    assert tuple(attempt) == ("codex-mini", "codex")
    _run(coordinator.runner.resume("operator"))
    assert coordinator.runner._state() is JobState.sample_ready
    coordinator.runner.on_sample_approved(1)
    _run(coordinator.runner.start_full_run())
    workbook = load_workbook(
        coordinator.job_dir / "output" / "results.v1.xlsx", read_only=True, data_only=True
    )
    try:
        rows = list(workbook["Results"].values)
    finally:
        workbook.close()
    headers = [str(value) for value in rows[0]]
    lead_index = headers.index("lead id")
    model_index = headers.index("model used")
    row = next(row for row in rows[1:] if str(row[lead_index]) == "10")
    assert row[model_index] == "codex-mini"


def test_model_unavailable_pause_keeps_inflight_rerouted_answer(
    coordinator: CoordinatorFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _real_pool(
        coordinator,
        tmp_path,
        monkeypatch,
        {"reroute": "10", "models": ["codex", "codex-mini"]},
    )
    _approve_plan(coordinator)

    _run(coordinator.runner.run_sample())

    attempt = coordinator.store.rows(
        "SELECT outcome, counted, model_used, rerouted_from FROM attempts WHERE job_id='job' AND lead_id='10'"
    )
    assert [tuple(row) for row in attempt] == [("ok", 1, "codex-mini", "codex")]
    assert [
        tuple(row)
        for row in coordinator.store.rows("SELECT status FROM leads WHERE job_id='job' AND lead_id='10'")
    ] == [("done",)]


def test_reroute_rate_over_one_percent_pauses(
    coordinator: CoordinatorFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _real_pool(coordinator, tmp_path, monkeypatch, {"reroute": "10", "models": ["codex", "codex-mini"]})
    _approve_plan(coordinator)

    _run(coordinator.runner.run_sample())

    assert coordinator.runner._state() is JobState.paused
    assert coordinator.store.rows("SELECT pause_reason FROM jobs WHERE id='job'")[0][0] == "model_unavailable"


def test_reroute_rate_baseline_resets_after_operator_resume(
    coordinator: CoordinatorFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _real_pool(
        coordinator,
        tmp_path,
        monkeypatch,
        {"reroute": ["10", "20"], "reroute_once": True, "models": ["codex", "codex-mini"]},
    )
    _approve_plan(coordinator)

    _run(coordinator.runner.run_sample())
    assert coordinator.runner._state() is JobState.paused
    _run(coordinator.runner.resume("operator"))

    assert coordinator.runner._state() is JobState.sample_ready
    coordinator.runner.on_sample_approved(1)
    _run(coordinator.runner.start_full_run())

    assert coordinator.runner._state() is JobState.paused
    assert coordinator.store.rows("SELECT pause_reason FROM jobs WHERE id='job'")[0][0] == "model_unavailable"


def test_reroute_rate_denominator_on_large_job(
    coordinator: CoordinatorFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extra_ids = [str(index + 40) for index in range(120)]
    ids = [str(row[0]) for row in coordinator.store.rows("SELECT lead_id FROM leads WHERE job_id='job'")]
    all_ids = ids + extra_ids
    (coordinator.job_dir / "ids.txt").write_text("\n".join(all_ids) + "\n", encoding="utf-8", newline="\n")
    preflight = list(coordinator.bq.preflight_pages[0] if coordinator.bq.preflight_pages else [])
    calls = list(coordinator.bq.pages[0])
    for lead_id in extra_ids:
        preflight.append(
            {
                "lead_id": lead_id,
                "found": True,
                "calls": 1,
                "usable_calls": 1,
                "transcript_calls": 1,
                "transcription_failed": None,
            }
        )
        calls.append(
            {
                "lead_id": lead_id,
                "call_id": f"call-{lead_id}-synthetic-0",
                "transcript_text": f"Synthetic Name says hello. zzlead{lead_id}zz " + ("x" * 25),
                "call_start": "synthetic-start-0",
                "duration": 200,
            }
        )
    coordinator.bq.pages = [calls]
    coordinator.bq.preflight_pages = [preflight]
    original_masking_context = coordinator.runner.deps.masking_context

    def masking_context(*args: Any) -> Any:
        return replace(original_masking_context(*args), allow_ids=frozenset(all_ids))

    coordinator.runner.deps = replace(coordinator.runner.deps, masking_context=masking_context)
    source = cast(LeadSourceIds, coordinator.runner.spec.lead_source)
    updated_spec = replace(
        coordinator.runner.spec,
        lead_source=replace(source, count=len(all_ids)),
    )
    # save_spec always writes the next version file, so the job must move to it.
    save_spec(updated_spec, coordinator.job_dir)
    edit_spec(coordinator.store, "job", 2)
    _real_pool(
        coordinator,
        tmp_path,
        monkeypatch,
        {
            # Leads are claimed in lead_id TEXT order, so "97" and "99" come last: more than 100
            # leads have finished before the first reroute, and "98" finishes between the two.
            "reroute": ["97", "99"],
            "models": ["codex", "codex-mini"],
            "latency_ms": 0,
        },
    )
    coordinator.runner.deps = replace(coordinator.runner.deps, concurrency=1)
    coordinator.runner.on_plan_approved(2)
    assert coordinator.store.rows("SELECT COUNT(*) FROM leads WHERE job_id='job'")[0][0] == len(all_ids)
    _run(coordinator.runner.run_sample())
    coordinator.runner.on_sample_approved(2)
    _run(coordinator.runner.start_full_run())

    assert coordinator.runner._state() is JobState.paused
    assert coordinator.store.rows("SELECT pause_reason FROM jobs WHERE id='job'")[0][0] == "model_unavailable"
    rerouted = coordinator.store.rows(
        "SELECT data_json FROM job_events WHERE job_id='job' AND kind='reroute' ORDER BY id"
    )
    assert [json.loads(str(row[0]))["lead_id"] for row in rerouted] == ["97", "99"]
    # No pause after the first rerouted answer: "97" kept its answer and "98" ran after it.
    assert (
        coordinator.store.rows("SELECT status FROM leads WHERE job_id='job' AND lead_id='97'")[0][0] == "done"
    )
    assert (
        coordinator.store.rows("SELECT status FROM leads WHERE job_id='job' AND lead_id='98'")[0][0] == "done"
    )
