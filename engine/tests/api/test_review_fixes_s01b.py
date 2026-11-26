"""Regression tests for the M2-S01b gate-1 review findings. Synthetic data only."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

import bca.api.production as production
import bca.outputs.excel as excel
from bca.api.production import ProductionBackend
from bca.api.server import RpcError
from bca.claude.auth import AuthStatus
from bca.config import LimitsConfig
from bca.coordinator.runner import JobRunner
from bca.luna.rpc import RpcError as AppServerRpcError
from bca.setup.retention import sweep
from bca.store import (
    CapRule,
    Condition,
    InScopeRule,
    JobSpec,
    JobState,
    LeadSourceIds,
    PauseReason,
    UsableRule,
    open_store,
    save_spec,
)
from bca.store.lifecycle import pause
from bca.store.spec import ReviewFlagRule, ThresholdGroupRule
from tests.api.t04_lifecycle import JOB, _wait_state, run_lifecycle
from tests.api.test_production import _write_job, make_backend, write_valid_config
from tests.api.test_quota_resume import _Clock
from tests.fakes.fake_bq import FakeBqRunner


def run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


class _FixedClock:
    def __init__(self, now: float = 5_000_000.0) -> None:
        self.value = now

    def now(self) -> float:
        return self.value

    def monotonic(self) -> float:
        return self.value


def _quota_event(backend: ProductionBackend, used: int, taken_at: float) -> None:
    # The exact shape runner.on_quota stores.
    backend.store.append_event(
        JOB,
        "quota",
        {
            "decision": "ALLOW",
            "reason": "quota is available",
            "limitId": "codex",
            "primary": {"usedPercent": used, "windowDurationMins": 300, "resetsAt": 9_999_999_999},
            "secondary": None,
            "creditsBalance": None,
            "hasCredits": False,
            "spendControlReached": None,
            "rateLimitReachedType": None,
            "ordinaryUsageAllowed": True,
            "planType": "pro",
            "takenAt": taken_at,
        },
    )


def _quota_used(event_json: str) -> int:
    value = json.loads(event_json)
    primary = value.get("primary") if isinstance(value, dict) else None
    return int(primary["usedPercent"]) if isinstance(primary, dict) else -1


# Major 1: job/snapshot must not crash while ChatGPT usage rises during a live run.
# A whole lifecycle plus a live full run: slow CI runners need more than the default 60 s.
@pytest.mark.timeout(240)
def test_snapshot_mid_run_with_rising_quota_succeeds(tmp_path: Path) -> None:
    async def scenario() -> None:
        lifecycle = await run_lifecycle(tmp_path, latency_ms=200, start_full=False)
        backend = lifecycle.backend
        scenario_path = tmp_path / "projects" / "project-one" / "jobs" / JOB / "fake-codex-scenario.json"
        value = json.loads(scenario_path.read_text(encoding="utf-8"))
        # The full run starts at 10% and every lead read adds 1%, as a filling 5-hour window does.
        value.update({"latency_ms": 2000, "used_percent": 10, "quota_increment": 1})
        scenario_path.write_text(json.dumps(value), encoding="utf-8", newline="\n")
        await backend.call("job/startFull", {"job": JOB})
        snapshot: dict[str, Any] | None = None
        for _ in range(3000):
            rows = backend.store.rows(
                "SELECT data_json FROM job_events WHERE job_id=? AND kind='quota' ORDER BY id", (JOB,)
            )
            rising = sorted({_quota_used(str(row[0])) for row in rows} - {-1})
            task = backend.jobs.tasks.get(JOB)
            if len([used for used in rising if used > 10]) >= 2 and task is not None and not task.done():
                snapshot = cast(dict[str, Any], await backend.call("job/snapshot", {"job": JOB}))
                break
            await asyncio.sleep(0.02)
        assert snapshot is not None, "the run never reported rising usage while it was still running"
        quota = snapshot["quota"]
        used = quota["codex"]["primary"]["usedPercent"]
        assert used > 10
        projected = quota["projectedEndPercent"]
        assert projected is None or used <= projected <= 100
        await _wait_state(backend, {"done", "done_with_issues"})

    run(scenario())


def _live_snapshot_backend(tmp_path: Path) -> ProductionBackend:
    backend = make_backend(tmp_path, bq=FakeBqRunner(), clock=_FixedClock())
    _write_job(backend, JOB, JobState.full_running)
    return backend


async def _snapshot_while_running(backend: ProductionBackend) -> dict[str, Any]:
    runner = backend.jobs.runner(JOB)
    assert runner is backend.jobs.runners[JOB]
    release = asyncio.Event()

    async def driver() -> None:
        await release.wait()

    backend.jobs.tasks[JOB] = asyncio.create_task(driver())
    try:
        return cast(dict[str, Any], await backend.call("job/snapshot", {"job": JOB}))
    finally:
        release.set()
        await backend.jobs.tasks.pop(JOB)


# Major 2: the projection is used + leads still to read x percent per completed lead.
def test_projected_end_percent_uses_percent_per_completed_lead(tmp_path: Path) -> None:
    backend = _live_snapshot_backend(tmp_path)
    for index in range(3):
        backend.store.add_lead(JOB, f"queued-{index}", "queued")
    backend.store.add_lead(JOB, "in-flight-1", "in_flight")
    _quota_event(backend, 40, 1_000)
    _quota_event(backend, 50, 2_000)
    runner = backend.jobs.runner(JOB)
    # This runner has seen every completion since the job began (no restart).
    runner._eta.observed_since = 0
    # Five leads finished while usage went from 40% to 50%: 2% per lead. Four leads are left.
    for ts in (1_100, 1_300, 1_500, 1_700, 1_900):
        runner._eta.completed(ts)
    snapshot = run(_snapshot_while_running(backend))
    assert snapshot["quota"]["projectedEndPercent"] == 50 + 4 * 2


def test_projected_end_percent_is_capped_at_100(tmp_path: Path) -> None:
    backend = _live_snapshot_backend(tmp_path)
    for index in range(40):
        backend.store.add_lead(JOB, f"queued-{index}", "queued")
    _quota_event(backend, 40, 1_000)
    _quota_event(backend, 50, 2_000)
    runner = backend.jobs.runner(JOB)
    runner._eta.observed_since = 0
    for ts in (1_100, 1_500):
        runner._eta.completed(ts)
    snapshot = run(_snapshot_while_running(backend))
    assert snapshot["quota"]["projectedEndPercent"] == 100


def test_projected_end_percent_after_a_restart_uses_only_what_this_runner_saw(tmp_path: Path) -> None:
    # Gate 2 of #32: quota events survive a restart in the store, but the rebuilt runner's
    # completions start empty, so pre-restart usage was divided by post-restart leads.
    backend = _live_snapshot_backend(tmp_path)
    for index in range(20):
        backend.store.add_lead(JOB, f"queued-{index}", "queued")
    _quota_event(backend, 10, 1_000)
    _quota_event(backend, 30, 1_500)
    _quota_event(backend, 50, 2_000)
    runner = backend.jobs.runner(JOB)
    assert runner._eta._completed == []
    runner._eta.observed_since = 2_400  # the engine came back at 2,400
    runner._eta.completed(2_900)
    _quota_event(backend, 51, 2_600)
    _quota_event(backend, 53, 3_000)
    snapshot = run(_snapshot_while_running(backend))
    # Since the restart: one lead read while usage went from 51% to 53%, so 2% per lead.
    assert snapshot["quota"]["projectedEndPercent"] == 53 + 20 * 2


def test_a_new_runner_counts_quota_from_when_it_started(tmp_path: Path) -> None:
    backend = _live_snapshot_backend(tmp_path)
    runner = backend.jobs.runner(JOB)
    # The runner's own clock (a fixed test clock here), the one quota events are stamped with.
    assert runner._eta.observed_since == runner._ts()


def test_projected_end_percent_is_unknown_until_a_lead_completes(tmp_path: Path) -> None:
    backend = _live_snapshot_backend(tmp_path)
    backend.store.add_lead(JOB, "queued-1", "queued")
    _quota_event(backend, 40, 1_000)
    _quota_event(backend, 50, 2_000)
    snapshot = run(_snapshot_while_running(backend))
    assert snapshot["quota"]["projectedEndPercent"] is None
    assert snapshot["quota"]["codex"]["primary"]["usedPercent"] == 50


# Major 3: rule summaries read like the rule.
def test_rule_summary_joins_any_rules_with_or() -> None:
    rule = InScopeRule("any", (Condition("answer.x", "eq", "a"), Condition("answer.y", "eq", "b")))
    assert ProductionBackend._rule_summary(rule)["summary"] == "In scope if answer.x is a or answer.y is b"
    flag = ReviewFlagRule("flag", "all", (Condition("answer.x", "eq", "a"), Condition("answer.y", "ne", "b")))
    assert ProductionBackend._rule_summary(flag)["summary"] == "Flag if answer.x is a and answer.y is not b"


def test_rule_summary_uses_plain_operator_words_and_no_value_for_null_checks() -> None:
    flag = ReviewFlagRule(
        "flag",
        "any",
        (
            Condition("answer.x", "is_null"),
            Condition("answer.y", "not_null"),
            Condition("answer.z", "in", ["a", "b"]),
            Condition("answer.w", "not_in", ["c"]),
            Condition("answer.n", "le", 5),
            Condition("answer.m", "gt", 1.5),
        ),
    )
    assert ProductionBackend._rule_summary(flag)["summary"] == (
        "Flag if answer.x is empty or answer.y is not empty or answer.z is one of a, b"
        " or answer.w is not one of c or answer.n <= 5 or answer.m > 1.5"
    )


def test_rule_summary_bands_follow_inclusive_upper() -> None:
    inclusive = ThresholdGroupRule("size", "answer.n", (1, 5), ("low", "mid", "high"), True)
    assert ProductionBackend._rule_summary(inclusive)["summary"] == (
        "Bands for answer.n: low (<= 1), mid (> 1 and <= 5), high (> 5)"
    )
    exclusive = ThresholdGroupRule("size", "answer.n", (1, 5), ("low", "mid", "high"), False)
    assert ProductionBackend._rule_summary(exclusive)["summary"] == (
        "Bands for answer.n: low (< 1), mid (>= 1 and < 5), high (>= 5)"
    )


# Major 4: a passed gate step carries its approval time, and the start card gets approvedAt.
def test_gate_steps_show_the_approval_times(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    _write_job(backend, JOB, JobState.full_running)
    for state in ("plan_proposed", "plan_approved", "sample_ready", "sample_approved", "full_running"):
        backend.store.append_event(JOB, "state", {"from": "", "to": state})
    plan_at, sample_at = 1_700_000_100, 1_700_000_500
    with backend.store.transaction() as cur:
        cur.execute(
            "UPDATE jobs SET plan_approved_at=?, sample_approved_at=? WHERE id=?",
            (
                datetime.fromtimestamp(plan_at, UTC).isoformat(),
                datetime.fromtimestamp(sample_at, UTC).isoformat(),
                JOB,
            ),
        )
    snapshot = cast(dict[str, Any], run(backend.call("job/snapshot", {"job": JOB})))
    steps = {step["id"]: step for step in snapshot["steps"]}
    assert steps["plan_approved"]["status"] == "done"
    assert steps["plan_approved"]["ts"] == snapshot["planApprovedAt"] == plan_at
    assert steps["sample_approved"]["status"] == "done"
    assert steps["sample_approved"]["ts"] == snapshot["sampleApprovedAt"] == sample_at
    assert snapshot["approvedAt"] == sample_at


def test_waiting_sample_gate_has_no_approved_at(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    _write_job(backend, JOB, JobState.sample_ready)
    backend.store.append_event(JOB, "state", {"from": "sample_running", "to": "sample_ready"})
    snapshot = cast(dict[str, Any], run(backend.call("job/snapshot", {"job": JOB})))
    step = next(item for item in snapshot["steps"] if item["id"] == "sample_approved")
    assert step["status"] == "waiting_for_you" and isinstance(step["ts"], int)
    assert "approvedAt" not in snapshot and snapshot["sampleApprovedAt"] is None


class _FakeClient:
    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state

    async def request(self, method: str, params: Any = None) -> dict[str, Any]:
        self.state["calls"].append(method)
        failure = self.state.get("fail", {}).get(method)
        if failure is not None:
            raise failure
        if method == "account/read":
            if not self.state.get("logged_in", True):
                return {"account": None}
            return {"account": self.state.get("account", {"type": "chatgpt", "planType": "plus"})}
        return {"rateLimits": {"primary": {"usedPercent": 20, "windowDurationMins": 300, "resetsAt": 1}}}


class _FakeServer:
    def __init__(self, state: dict[str, Any]) -> None:
        self.client = _FakeClient(state)

    async def stop(self) -> None:
        return None


def _fake_launch(state: dict[str, Any]) -> Any:
    state.setdefault("calls", [])

    async def launch() -> _FakeServer:
        return _FakeServer(state)

    return launch


def _signed_out_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(production, "claude_auth_status", lambda *_: AuthStatus(False, None, None))


def _check(result: dict[str, Any], name: str) -> dict[str, Any]:
    return next(item for item in result["items"] if item["name"] == name)


# Major 5: allowance/status reports codex null when Codex cannot start or answer.
def test_allowance_status_is_codex_null_when_codex_cannot_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BCA_TEST_FAKES", raising=False)
    monkeypatch.setenv("BCA_CODEX_BIN", str(tmp_path / "missing" / "codex-synthetic.exe"))
    backend = make_backend(tmp_path)
    assert run(backend.call("allowance/status", {})) == {"codex": None, "claude": None}


def test_allowance_status_is_codex_null_when_the_rate_limit_read_fails(tmp_path: Path) -> None:
    state: dict[str, Any] = {"fail": {"account/rateLimits/read": AppServerRpcError(-32600, "signed out")}}
    backend = make_backend(tmp_path, launch_pool=_fake_launch(state))
    result = cast(dict[str, Any], run(backend.call("allowance/status", {})))
    assert result["codex"] is None


# Major 6: an error raised after the automatic resume moved the job on stalls the job.
def test_automatic_resume_error_after_resume_stalls_the_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing_phase(self: JobRunner, sample: bool, canary: bool = False) -> None:
        raise RuntimeError("synthetic failure after resume")

    monkeypatch.setattr(JobRunner, "_run_phase", failing_phase)

    async def scenario() -> None:
        backend = make_backend(tmp_path, bq=FakeBqRunner())
        clock = _Clock(backend)
        backend.clock = clock
        _write_job(backend, JOB, JobState.full_running)
        pause(backend.store, JOB, PauseReason.quota, int(clock.now()))
        events = backend.event_bus.subscribe()
        watcher = asyncio.create_task(backend._resume_watcher())
        for _ in range(20_000):
            if backend.store.rows("SELECT state FROM jobs WHERE id=?", (JOB,))[0][0] == JobState.stalled:
                break
            await asyncio.sleep(0)
        backend._stopping = True
        await asyncio.wait_for(watcher, 10)
        assert backend.store.rows("SELECT state FROM jobs WHERE id=?", (JOB,))[0][0] == JobState.stalled
        assert any(event.kind == "stall" for event in events.drain())

    run(asyncio.wait_for(scenario(), 30))


def test_refused_automatic_resume_through_the_job_manager_stays_paused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refuse(self: JobRunner, actor: str = "operator") -> None:
        raise PermissionError("synthetic refusal before resuming")

    monkeypatch.setattr(JobRunner, "resume", refuse)

    async def scenario() -> None:
        backend = make_backend(tmp_path, bq=FakeBqRunner())
        clock = _Clock(backend)
        backend.clock = clock
        _write_job(backend, JOB, JobState.full_running)
        due = int(clock.now())
        pause(backend.store, JOB, PauseReason.quota, due)
        watcher = asyncio.create_task(backend._resume_watcher())
        for _ in range(20_000):
            task = backend.jobs.tasks.get(JOB)
            if task is not None and task.done():
                break
            await asyncio.sleep(0)
        backend._stopping = True
        await asyncio.wait_for(watcher, 10)
        task = backend.jobs.tasks[JOB]
        assert task.done() and task.exception() is None
        state, reason, resume_at = backend.store.rows(
            "SELECT state, pause_reason, resume_at FROM jobs WHERE id=?", (JOB,)
        )[0]
        assert (state, reason) == (JobState.paused, PauseReason.quota)
        assert int(resume_at) >= due + 600

    run(asyncio.wait_for(scenario(), 30))


# Major 7: an explicit worker count argument wins over the saved one.
def test_explicit_concurrency_argument_wins_over_saved_worker_count(tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text(json.dumps({"workerCount": 3}), encoding="utf-8")
    assert make_backend(tmp_path).concurrency == 3
    assert make_backend(tmp_path, concurrency=2).concurrency == 2
    (tmp_path / "settings.json").write_text(json.dumps({"workerCount": None}), encoding="utf-8")
    assert make_backend(tmp_path).concurrency == 4


class _LoginProcess:
    pid = 999_999

    def __init__(self) -> None:
        self.exit_code: int | None = None

    def poll(self) -> int | None:
        return self.exit_code


# Major 8: a ChatGPT sign-in is seen at once, not after the 300 s allowance cache expires.
def test_chatgpt_sign_in_is_seen_while_the_login_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state: dict[str, Any] = {"logged_in": False}
    clock = _FixedClock()
    backend = make_backend(tmp_path, launch_pool=_fake_launch(state), clock=clock)
    login = _LoginProcess()
    _signed_out_claude(monkeypatch)
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: login)
    before = cast(dict[str, Any], run(backend.call("auth/status", {})))
    assert before["codex"]["loggedIn"] is False
    assert run(backend.call("auth/codexLogin", {})) == {"started": True, "vendor": "codex"}
    state["logged_in"] = True
    clock.value += 5
    checks = cast(dict[str, Any], run(backend.call("setup/runChecks", {})))
    assert _check(checks, "chatgpt_login")["status"] == "ok"
    login.exit_code = 0
    clock.value += 10
    after = cast(dict[str, Any], run(backend.call("auth/status", {})))
    assert after["codex"]["loggedIn"] is True


def test_chatgpt_sign_in_is_seen_once_the_login_ends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state: dict[str, Any] = {"logged_in": False}
    clock = _FixedClock()
    backend = make_backend(tmp_path, launch_pool=_fake_launch(state), clock=clock)
    login = _LoginProcess()
    _signed_out_claude(monkeypatch)
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: login)
    run(backend.call("auth/codexLogin", {}))
    clock.value += 5
    # Read while the browser sign-in is still open: not signed in yet.
    checks = cast(dict[str, Any], run(backend.call("setup/runChecks", {})))
    assert _check(checks, "chatgpt_login")["status"] == "failed"
    state["logged_in"] = True
    login.exit_code = 0
    clock.value += 10
    after = cast(dict[str, Any], run(backend.call("auth/status", {})))
    assert after["codex"]["loggedIn"] is True
    reads = state["calls"].count("account/read")
    clock.value += 10
    run(backend.call("auth/status", {}))
    assert state["calls"].count("account/read") == reads, "the cache applies again after the sign-in"


# Major 9: no site config yet (first run) must not break the Codex read or the checklist.
def _no_config_backend(tmp_path: Path, state: dict[str, Any]) -> ProductionBackend:
    backend = ProductionBackend(
        tmp_path, store=open_store(Path(":memory:")), launch_pool=_fake_launch(state), clock=_FixedClock()
    )
    assert backend.config is None
    return backend


def test_codex_reads_work_without_site_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _signed_out_claude(monkeypatch)
    backend = _no_config_backend(tmp_path, {})
    checks = cast(dict[str, Any], run(backend.call("setup/runChecks", {})))
    assert _check(checks, "chatgpt_login")["status"] == "ok"
    auth = cast(dict[str, Any], run(backend.call("auth/status", {})))
    assert auth["codex"]["loggedIn"] is True
    allowance = cast(dict[str, Any], run(backend.call("allowance/status", {})))
    assert allowance["codex"]["stopAtPercent"] == LimitsConfig().codex_stop_percent


def test_run_checks_turns_any_codex_probe_failure_into_a_failed_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _signed_out_claude(monkeypatch)
    backend = _no_config_backend(tmp_path, {"fail": {"account/read": ValueError("synthetic bad reply")}})
    checks = cast(dict[str, Any], run(backend.call("setup/runChecks", {})))
    row = _check(checks, "chatgpt_login")
    assert row["status"] == "failed" and row["detail"] == "Not signed in yet"


# Minor: the retention days saved in Settings are what retention and chat history use.
def test_saved_retention_days_drive_retention_and_chat_history(tmp_path: Path) -> None:
    write_valid_config(tmp_path / "site.toml")
    clock = _FixedClock(1_800_000_000.0)
    backend = make_backend(tmp_path, clock=clock)
    run(backend.call("settings/set", {"retentionDays": 12}))
    retention = backend._retention()
    assert (retention.masked_cache_days, retention.chat_days) == (12, 12)
    run(backend.call("projects/create", {"project": "project-one", "title": "Synthetic"}))
    day = 86_400
    backend._chat_history_path("project-one").write_text(
        "".join(
            json.dumps(
                {"id": index, "ts": int(clock.value) - days * day, "role": "user", "text": "synthetic"}
            )
            + "\n"
            for index, days in ((1, 13), (2, 11))
        ),
        encoding="utf-8",
    )
    history = cast(dict[str, Any], run(backend.call("chat/history", {"project": "project-one"})))
    assert [entry["id"] for entry in history["entries"]] == [2]
    raw = tmp_path / "raw"
    raw.mkdir()
    finished = datetime.fromtimestamp(clock.value, UTC)
    for name, days in (("old.txt", 13), ("new.txt", 11)):
        (raw / name).write_text("synthetic", encoding="utf-8")
        stamp = (finished - timedelta(days=days)).timestamp()
        os.utime(raw / name, (stamp, stamp))
    candidates = sweep(tmp_path, backend._retention(), finished_at=finished, dry_run=True)
    assert [path.name for path in candidates] == ["old.txt"]
    restarted = make_backend(tmp_path)
    assert restarted._retention().chat_days == 12
    assert cast(dict[str, Any], run(restarted.call("settings/get", {})))["retentionDays"] == 12


def _write_job_with_answer_schema(backend: ProductionBackend) -> JobSpec:
    backend.store.create_project("project-one", "Synthetic")
    backend.store.create_job(JOB, "project-one", 1, JobState.done)
    spec = JobSpec(
        1,
        JOB,
        "project-one",
        "Synthetic job",
        "Synthetic question",
        LeadSourceIds("ids.txt", 0),
        usable=UsableRule(min_chars=1),
        cap=CapRule(100, "drop_short_then_oldest", 180),
        instructions="Return the answer.",
        output_schema={"type": "object", "properties": {"answer": {"type": "string"}}},
        luna_model="codex",
        luna_effort="low",
        sample_size=1,
    )
    save_spec(spec, backend.app_data / "projects" / "project-one" / "jobs" / JOB)
    return spec


def _output_names(backend: ProductionBackend) -> list[str]:
    files = cast(list[dict[str, object]], backend._job_outputs(JOB)["files"])
    return sorted(str(item["name"]) for item in files)


# Minor: job/outputs lists only finished workbooks.
def test_job_outputs_skips_temporary_and_reserved_workbooks(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    _write_job_with_answer_schema(backend)
    output = tmp_path / "projects" / "project-one" / "jobs" / JOB / "output"
    output.mkdir(parents=True)
    (output / "results.v1.xlsx").write_bytes(b"synthetic workbook")
    (output / ".results.v1.rebuild1.tmp123.xlsx").write_bytes(b"synthetic partial workbook")
    (output / "results.v1.rebuild1.xlsx").write_bytes(b"")
    assert _output_names(backend) == ["results.v1.xlsx"]


def test_job_outputs_never_lists_a_rebuild_while_it_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = make_backend(tmp_path, bq=FakeBqRunner())
    spec = _write_job_with_answer_schema(backend)
    output = tmp_path / "projects" / "project-one" / "jobs" / JOB / "output"
    excel.build_workbook(backend.store, JOB, spec, output, 1)
    seen_while_writing: list[list[str]] = []
    write_side_sheets = excel._write_side_sheets

    def recording(path: Path, *args: Any, **kwargs: Any) -> None:
        seen_while_writing.append(_output_names(backend))
        write_side_sheets(path, *args, **kwargs)

    monkeypatch.setattr(excel, "_write_side_sheets", recording)
    rule = ReviewFlagRule("synthetic_flag", "all", (Condition("answer", "eq", "synthetic"),))
    backend.jobs.runner(JOB).rebuild_with_rules((rule,))
    assert seen_while_writing == [["results.v1.xlsx"]]
    assert _output_names(backend) == ["results.v1.rebuild1.xlsx", "results.v1.xlsx"]


# Minor: a failed chat turn stores fixed words, never the raw error text.
@pytest.mark.parametrize(
    ("reason", "text"),
    [
        ("claude_turn_failed", "Turn failed: Claude could not finish this reply."),
        ("claude_limit_reached", "Turn failed: the Claude allowance is used up for now."),
        ("synthetic_unknown_reason", "Turn failed."),
    ],
)
def test_turn_failed_entry_has_plain_words_not_the_raw_error(tmp_path: Path, reason: str, text: str) -> None:
    backend = make_backend(tmp_path)
    run(backend.call("projects/create", {"project": "project-one", "title": "Synthetic"}))
    raw = r"synthetic raw detail C:\Users\synthetic\secret.txt 9876543210"

    async def failing_turn(_project: str, _text: str) -> dict[str, object]:
        raise production.ClaudeTurnError(raw, reason)

    backend._chat_turn = failing_turn  # type: ignore[assignment, method-assign]
    with pytest.raises(RpcError):
        run(backend.call("chat/send", {"project": "project-one", "text": "hello"}))
    stored = backend._chat_history_path("project-one").read_text(encoding="utf-8")
    entry = json.loads(stored.splitlines()[-1])
    assert entry["role"] == "status" and entry["text"] == text
    assert "synthetic raw detail" not in stored and "9876543210" not in stored
