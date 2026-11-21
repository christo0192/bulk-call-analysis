from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest

from bca.coordinator.runner import _Callbacks
from bca.monitor.liveness import Liveness
from bca.monitor.progress import counts
from bca.outputs.excel import build_workbook
from bca.store import JobState, LeadStatus, open_store
from tests.api.test_production import _tiny_job, _write_job, make_backend, run
from tests.coordinator.conftest import FakeClock
from tests.fakes.fake_bq import FakeBqRunner


def test_progress_excludes_unusable_leads_from_total_and_remaining() -> None:
    store = open_store(Path(":memory:"))
    store.create_project("project", "Synthetic")
    store.create_job("job", "project", 1, JobState.full_running)
    store.add_lead("job", "done", LeadStatus.done, in_sample=0)
    for lead, status in (
        ("empty", LeadStatus.no_transcript),
        ("short", LeadStatus.too_short),
        ("missing", LeadStatus.not_found),
    ):
        store.add_lead("job", lead, status, in_sample=0)
    result = counts(store, "job", sample=False)
    assert result.total == 1
    assert result.remaining == 0


def test_progress_identity_holds_in_finished_and_paused_jobs() -> None:
    store = open_store(Path(":memory:"))
    store.create_project("project", "Synthetic")
    store.create_job("job", "project", 1, JobState.paused)
    for lead, status in (
        ("done", LeadStatus.done),
        ("queued", LeadStatus.queued),
        ("failed", LeadStatus.failed),
        ("held", LeadStatus.held_back),
    ):
        store.add_lead("job", lead, status, in_sample=0)
    for state in (JobState.paused, JobState.done):
        store.rows("UPDATE jobs SET state=? WHERE id=?", (state, "job"))
        result = counts(store, "job", sample=False)
        assert result.total == (
            result.done
            + result.in_flight
            + result.retrying
            + sum(result.failed.values())
            + result.held_back
            + result.remaining
        )


def test_heartbeat_restarts_after_sleep_wake_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backend, _ = _tiny_job(tmp_path, monkeypatch, latency_ms=60000)
    clock = FakeClock()
    backend.clock = clock
    runner = backend.jobs.runner("job-one")

    async def scenario() -> None:
        runner._phase_running = True
        runner._start_heartbeat()
        first = runner._heartbeat_task
        await runner._stop_heartbeat()
        runner._phase_running = True
        runner._start_heartbeat()
        second = runner._heartbeat_task
        assert first is not second
        await runner._stop_heartbeat()

    run(scenario())


def test_heartbeat_is_never_started_twice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backend, _ = _tiny_job(tmp_path, monkeypatch, latency_ms=60000)
    runner = backend.jobs.runner("job-one")

    async def scenario() -> None:
        runner._phase_running = True
        runner._start_heartbeat()
        first = runner._heartbeat_task
        runner._start_heartbeat()
        assert runner._heartbeat_task is first
        await runner._stop_heartbeat()

    run(scenario())


def test_last_activity_ignores_pool_timer_and_tracks_codex_events() -> None:
    liveness = Liveness(lambda: 4, 2)
    liveness.lead_started("lead", 10)
    liveness.check(20)
    assert liveness.last_activity == 10
    liveness.on_activity(21)
    assert liveness.last_activity == 21


def test_check_tick_does_not_update_last_activity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backend = make_backend(tmp_path, bq=FakeBqRunner())
    _write_job(backend, state=JobState.sample_running)
    runner = backend.jobs.runner("job-one")
    callbacks = _Callbacks(runner)
    runner._last_activity_ts = 10.0
    runner._liveness.last_activity = 10.0

    async def scenario() -> None:
        await callbacks._check_monitors(11.0, activity=False)

    run(scenario())
    assert runner._last_activity_ts == 10.0


def test_silent_hang_raises_stall_with_check_tick() -> None:
    liveness = Liveness(lambda: 3, 2, pool_silence_s=30)
    for lead in ("a", "b", "c"):
        liveness.lead_started(lead, 0)
    liveness.on_activity(0)
    for timestamp in range(5, 31, 5):
        events = liveness.check(timestamp)
    assert any(event.kind == "stall" for event in events)


def test_liveness_recovers_from_sleeping_when_activity_resumes() -> None:
    liveness = Liveness(lambda: 1, 2)
    liveness.lead_started("lead", 10)
    liveness.sleep_detected(20)
    assert liveness.check(100) == []
    liveness.on_activity(101)
    assert liveness.last_activity == 101
    assert liveness.check(102) == []


def test_heartbeat_tick_error_is_logged_and_does_not_block_pause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    backend, _ = _tiny_job(tmp_path, monkeypatch, latency_ms=60000)
    backend.clock = FakeClock()
    runner = backend.jobs.runner("job-one")
    runner._phase_running = True
    monkeypatch.setattr(runner, "_publish_progress", lambda: (_ for _ in ()).throw(RuntimeError("synthetic")))

    async def scenario() -> None:
        with caplog.at_level(logging.ERROR):
            task = asyncio.create_task(runner._heartbeat())
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert "heartbeat tick failed" in caplog.text

    run(scenario())


def test_cancelling_a_running_phase_still_cleans_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backend, _ = _tiny_job(tmp_path, monkeypatch, latency_ms=60000)
    backend.store.rows("UPDATE jobs SET state='sample_running' WHERE id='job-one'")
    runner = backend.jobs.runner("job-one")

    async def scenario() -> None:
        task = asyncio.create_task(runner._run_phase(True))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert runner._phase_running is False
        await asyncio.wait_for(backend.call("job/pause", {"job": "job-one"}), 1)

    run(scenario())


def test_paused_sample_reports_sample_phase_counts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backend, _ = _tiny_job(tmp_path, monkeypatch, latency_ms=1)
    runner = backend.jobs.runner("job-one")
    backend.store.rows("UPDATE jobs SET state='paused' WHERE id='job-one'")
    backend.store.rows("UPDATE jobs SET resume_state='sample_running' WHERE id='job-one'")
    subscription = backend.event_bus.subscribe()
    runner._publish_progress()
    event = subscription.drain()[0]
    assert event.data["phase"] == "sample"


def test_approved_bytes_follow_the_approved_spec_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend, job_dir = _tiny_job(tmp_path, monkeypatch, latency_ms=1)
    runner = backend.jobs.runner("job-one")
    (job_dir / "preflight.v1.json").write_text(json.dumps({"dry_run_bytes": 11}), encoding="utf-8")
    assert runner._approved_bytes() == 11


def test_approved_bytes_apply_only_to_the_pull_query(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backend, job_dir = _tiny_job(tmp_path, monkeypatch, latency_ms=1)
    runner = backend.jobs.runner("job-one")
    (job_dir / "preflight.v1.json").write_text(json.dumps({"dry_run_bytes": 11}), encoding="utf-8")
    import bca.coordinator.runner as runner_module

    seen: dict[str, object] = {}

    def fake_preflight(*args: object, **kwargs: object) -> object:
        seen["preflight_approved"] = args[9]
        return None

    def fake_pull(*args: object, **kwargs: object) -> object:
        seen["pull_approved"] = args[9]
        return None

    monkeypatch.setattr(runner_module, "run_preflight", fake_preflight)
    monkeypatch.setattr(runner_module, "pull_transcripts", fake_pull)
    runner._pull()
    assert seen == {"preflight_approved": None, "pull_approved": 11}


def test_concurrent_rebuilds_get_distinct_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backend, _ = _tiny_job(tmp_path, monkeypatch, latency_ms=1)
    runner = backend.jobs.runner("job-one")
    output = runner.deps.job_dir / "output"
    output.mkdir(parents=True, exist_ok=True)
    build_workbook(backend.store, "job-one", runner.spec, output, runner.spec.spec_version)

    async def scenario() -> list[Path]:
        return list(
            await asyncio.gather(*(asyncio.to_thread(runner.rebuild_with_rules, ()) for _ in range(2)))
        )

    names = run(scenario())
    assert len({Path(str(name)).name for name in names}) == 2


def test_unknown_rate_limit_reason_is_turn_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from bca.claude.stream import Event as ClaudeEvent
    from bca.claude.stream import RateLimitGuard

    decision = RateLimitGuard().inspect(ClaudeEvent("rate_limit", {"rate_limit_info": {"status": "mystery"}}))
    assert decision.blocked_reason == "unknown_rate_limit_status"


def test_rebuild_leaves_no_open_handles_on_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backend, _ = _tiny_job(tmp_path, monkeypatch, latency_ms=1)
    runner = backend.jobs.runner("job-one")
    output = runner.deps.job_dir / "output"
    output.mkdir(parents=True, exist_ok=True)
    build_workbook(backend.store, "job-one", runner.spec, output, runner.spec.spec_version)
    path = runner.rebuild_with_rules(())
    assert Path(path).is_file()
