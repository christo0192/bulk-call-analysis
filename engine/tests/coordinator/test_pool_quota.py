from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from openpyxl import load_workbook

import bca.coordinator.runner as runner_module
from bca.luna.appserver import AppServer, CodexLaunch, start
from bca.luna.pool import LunaPool
from bca.store import JobState, LeadStatus
from bca.store import start_full_run as start_full_run_job
from tests.coordinator.conftest import CoordinatorFixture
from tests.fakes import fake_codex


def _run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


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
