from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

import bca.coordinator.runner as runner_module
from bca.api.production import JobManager, ProductionBackend
from bca.api.server import ApiService, JsonRpcServer, RpcError
from bca.bq.runner import DryRun
from bca.config import (
    BigQueryConfig,
    LimitsConfig,
    MaskingConfig,
    ModelsConfig,
    RecipeConfig,
    RetentionConfig,
    SiteConfig,
)
from bca.coordinator.runner import JobRunner
from bca.mask.masker import mask_lead_stream
from bca.monitor.events import Event
from bca.store import JobSpec, JobState, LeadSourceIds, open_store, save_spec
from bca.store.spec import CapRule, UsableRule
from tests.api.test_stdio_chat import _create_project, _send, _start
from tests.coordinator.conftest import _synthetic_rows
from tests.fakes.fake_bq import FakeBqRunner


def run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


def make_backend(tmp_path: Path, **kwargs: Any) -> ProductionBackend:
    config = SiteConfig(
        BigQueryConfig("synthetic-project", key_path=tmp_path / "connector.json"),
        RecipeConfig(
            "dataset.transcripts", "dataset.calls", "lead_id", "call_id", "text", "started", "duration"
        ),
        ModelsConfig(),
        LimitsConfig(),
        RetentionConfig(),
        MaskingConfig(),
    )
    return ProductionBackend(tmp_path, config=config, store=open_store(Path(":memory:")), **kwargs)


def write_valid_config(path: Path) -> None:
    path.write_text(
        """[bigquery]
project = "synthetic-project"
[recipe]
transcripts_table = "dataset.transcripts"
calls_table = "dataset.calls"
lead_id_column = "lead_id"
call_id_column = "call_id"
transcript_text_column = "text"
call_start_column = "started"
call_duration_column = "duration"
[models]
[limits]
[retention]
[masking]
""",
        encoding="utf-8",
        newline="\n",
    )


def test_app_status_reports_data_dir_and_running_jobs(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    result = run(backend.call("app/status", {}))
    assert result["dataDir"] == str(tmp_path)
    assert result["runningJobs"] == []


def test_backend_starts_without_config_and_reports_setup_required(tmp_path: Path) -> None:
    backend = ProductionBackend(tmp_path, store=open_store(Path(":memory:")))
    assert run(backend.call("app/status", {}))["dataDir"] == str(tmp_path)
    with pytest.raises(RpcError) as error:
        run(backend.call("projects/list", {}))
    assert cast(dict[str, object], error.value.data) == {"reason": "setup_required"}


def test_backend_reports_invalid_config_without_failing_startup(tmp_path: Path) -> None:
    (tmp_path / "site.toml").write_text(
        """[bigquery]
project = "synthetic-project"
[recipe]
transcripts_table = "dataset.transcripts"
calls_table = "dataset.calls"
lead_id_column = "lead_id"
call_id_column = "call_id"
transcript_text_column = "text"
call_start_column = "started"
call_duration_column = "duration"
[models]
[retention]
[masking]
""",
        encoding="utf-8",
        newline="\n",
    )
    backend = ProductionBackend(tmp_path, store=open_store(Path(":memory:")))
    with pytest.raises(RpcError) as error:
        run(backend.call("projects/list", {}))
    data = cast(dict[str, object], error.value.data)
    assert data["reason"] == "setup_required"
    assert "limits: missing section" in cast(str, data["message"])


def test_stdio_engine_starts_without_config(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment["BCA_DATA_DIR"] = str(tmp_path)
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "app/status", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "projects/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "shutdown", "params": {}},
    ]
    process = subprocess.Popen(
        [sys.executable, "-m", "bca.api.stdio"],
        cwd=str(Path(__file__).parents[2]),
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdin is not None and process.stdout is not None
    process.stdin.write(json.dumps(requests[0]) + "\n")
    process.stdin.flush()
    status = json.loads(process.stdout.readline())
    assert status["result"]["state"] == "ready"
    assert process.poll() is None
    process.stdin.write(json.dumps(requests[1]) + "\n")
    process.stdin.flush()
    projects = json.loads(process.stdout.readline())
    assert projects["error"]["data"] == {"reason": "setup_required"}
    assert process.poll() is None
    process.stdin.write(json.dumps(requests[2]) + "\n")
    process.stdin.flush()
    shutdown = json.loads(process.stdout.readline())
    assert shutdown["result"] == {"stopping": True, "exitCode": 0}
    _, stderr = process.communicate(timeout=10)
    assert process.returncode == 0, stderr


def test_stdio_shutdown_exits_with_stdin_open(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment["BCA_DATA_DIR"] = str(tmp_path)
    process = subprocess.Popen(
        [sys.executable, "-m", "bca.api.stdio"],
        cwd=str(Path(__file__).parents[2]),
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdin is not None and process.stdout is not None
    process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "shutdown", "params": {}}) + "\n")
    process.stdin.flush()
    assert json.loads(process.stdout.readline())["result"]["stopping"] is True
    deadline = time.monotonic() + 3
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert process.poll() == 0
    process.communicate(timeout=1)


def test_failed_notification_gets_no_reply(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment["BCA_DATA_DIR"] = str(tmp_path)
    process = subprocess.Popen(
        [sys.executable, "-m", "bca.api.stdio"],
        cwd=str(Path(__file__).parents[2]),
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdin is not None and process.stdout is not None
    process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "no/such-method", "params": {}}) + "\n")
    process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "shutdown", "params": {}}) + "\n")
    process.stdin.flush()
    line = process.stdout.readline()
    assert line and json.loads(line)["id"] == 2
    process.communicate(timeout=3)


def test_config_written_after_start_is_loaded_without_restart(tmp_path: Path) -> None:
    backend = ProductionBackend(tmp_path, store=open_store(Path(":memory:")))
    write_valid_config(tmp_path / "site.toml")
    result = run(backend.call("settings/get", {}))
    assert result["claudeModel"] == "claude-opus-5-5"


def test_setup_checklist_uses_setup_service(tmp_path: Path) -> None:
    result = run(make_backend(tmp_path).call("setup/checklist", {}))
    assert any(item["name"] == "training_off" for item in result)


def test_setup_run_checks_and_manual_mark_are_distinct(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    checked = run(backend.call("setup/runChecks", {}))
    marked = run(backend.call("setup/markManual", {"item": "training_off"}))
    assert checked["items"]
    assert marked == {"item": "training_off", "status": "manual_done"}


def test_settings_get_only_reports_key_location(tmp_path: Path) -> None:
    key = tmp_path / "connector.json"
    key.write_text("synthetic-secret", encoding="utf-8")
    result = run(make_backend(tmp_path).call("settings/get", {}))
    assert result["keyLocated"] is True
    assert "synthetic-secret" not in str(result)


def test_settings_model_change_requires_confirmation(tmp_path: Path) -> None:
    with pytest.raises(RpcError) as error:
        run(make_backend(tmp_path).call("settings/set", {"codexModel": "new-model"}))
    assert (
        cast(dict[str, object], error.value.data)["reason"]
        == "confirmModelChange required to change model ids"
    )


def test_projects_create_and_list_use_store(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    run(backend.call("projects/create", {"project": "project-one", "title": "Synthetic"}))
    assert run(backend.call("projects/list", {})) == [{"project": "project-one", "title": "Synthetic"}]


def test_projects_reject_non_strict_slug(tmp_path: Path) -> None:
    with pytest.raises(RpcError) as error:
        run(make_backend(tmp_path).call("projects/create", {"project": "../escape", "title": "x"}))
    assert cast(dict[str, object], error.value.data)["reason"] == "invalid slug"


def test_files_list_is_scoped_to_project(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    run(backend.call("projects/create", {"project": "project-one", "title": "Synthetic"}))
    root = tmp_path / "projects" / "project-one"
    (root / "inside.txt").write_text("ok", encoding="utf-8")
    (root / "chat").mkdir()
    (root / "chat" / "mcp.json").write_text("{}", encoding="utf-8")
    (root / "jobs" / "job-one" / "raw").mkdir(parents=True)
    (root / "jobs" / "job-one" / "raw" / "lead.jsonl.gz").write_bytes(b"raw")
    (root / "jobs" / "job-one" / "output").mkdir(parents=True)
    (root / "jobs" / "job-one" / "output" / "results.xlsx").write_bytes(b"xlsx")
    (root / "harvested_names.json").write_text("[]", encoding="utf-8")
    (root / ".bca-marker").write_text("internal", encoding="utf-8")
    (root / "store.sqlite3").write_bytes(b"sqlite")
    (root / "codex-app-server.log").write_text("internal", encoding="utf-8")
    result = run(backend.call("files/list", {"project": "project-one"}))
    assert result == [
        {"name": "inside.txt", "size": 2},
        {"name": "jobs/job-one/output/results.xlsx", "size": 4},
    ]


def test_files_list_rejects_traversal(tmp_path: Path) -> None:
    with pytest.raises(RpcError) as error:
        run(make_backend(tmp_path).call("files/list", {"project": "../outside"}))
    assert cast(dict[str, object], error.value.data)["reason"] == "path outside project"


def _write_job(
    backend: ProductionBackend, job_id: str = "job-one", state: JobState = JobState.full_running
) -> Path:
    # A job belongs to a project row (foreign key); create the project once.
    if not backend.store.rows("SELECT 1 FROM projects WHERE id='project-one'"):
        backend.store.create_project("project-one", "Synthetic")
    backend.store.create_job(job_id, "project-one", 1, state)
    job_dir = backend.app_data / "projects" / "project-one" / job_id
    save_spec(
        JobSpec(
            1,
            job_id,
            "project-one",
            "Synthetic job",
            "Synthetic question",
            LeadSourceIds("ids.txt", 0),
            usable=UsableRule(min_chars=1),
            cap=CapRule(100, "drop_short_then_oldest", 180),
            instructions="Return the answer.",
            output_schema={"type": "object", "properties": {}},
            luna_model="codex",
            luna_effort="low",
            sample_size=1,
        ),
        job_dir,
    )
    return job_dir


def test_approve_plan_with_stale_version_returns_error(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    _write_job(backend, state=JobState.plan_proposed)
    before = tuple(backend.store.rows("SELECT state, spec_version FROM jobs WHERE id='job-one'")[0])
    with pytest.raises(RpcError):
        run(backend.call("job/approvePlan", {"job": "job-one", "expectedSpecVersion": 2}))
    after = tuple(backend.store.rows("SELECT state, spec_version FROM jobs WHERE id='job-one'")[0])
    assert after == before


def test_background_job_failure_stalls_job_and_publishes_event(tmp_path: Path) -> None:
    # Building a runner needs a BigQuery runner; use the fake instead of a connector file.
    backend = make_backend(tmp_path, bq=FakeBqRunner())
    _write_job(backend)
    events = backend.event_bus.subscribe()
    runner = backend.jobs.runner("job-one")

    async def fail(_runner: JobRunner) -> None:
        raise RuntimeError("synthetic runner failure")

    async def scenario() -> None:
        await backend.jobs.start("job-one", fail)
        for _ in range(20):
            await asyncio.sleep(0)
            if backend.store.rows("SELECT state FROM jobs WHERE id='job-one'")[0][0] == JobState.stalled:
                break

    run(scenario())
    assert backend.store.rows("SELECT state FROM jobs WHERE id='job-one'")[0][0] == JobState.stalled
    assert any(event.kind in {"stall", "notice"} for event in events.drain())
    with pytest.raises(RpcError):
        run(backend.call("job/resume", {"job": "job-one"}))
    assert runner.job_id == "job-one"


def test_engine_restart_resumes_running_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backend = make_backend(tmp_path)
    _write_job(backend)
    (tmp_path / ".bca-test-fakes").touch()

    async def finish(self: JobRunner) -> None:
        with self.deps.store.transaction() as cursor:
            cursor.execute("UPDATE jobs SET state='done' WHERE id=?", (self.job_id,))

    monkeypatch.setattr(JobRunner, "start_full_run", finish)
    recovered = ProductionBackend(tmp_path, config=backend.config, store=backend.store, bq=object())

    async def wait_for_recovery() -> None:
        await recovered.call("app/status", {})
        for _ in range(100):
            if recovered.store.rows("SELECT state FROM jobs WHERE id='job-one'")[0][0] == JobState.done:
                return
            await asyncio.sleep(0.01)
        raise AssertionError("recovered job did not finish")

    run(wait_for_recovery())
    assert recovered.store.rows("SELECT state FROM jobs WHERE id='job-one'")[0][0] == JobState.done


def test_job_progress_and_state_notifications_are_emitted(tmp_path: Path) -> None:
    backend = make_backend(tmp_path, bq=FakeBqRunner())
    _write_job(backend)
    subscription = backend.event_bus.subscribe()
    runner = backend.jobs.runner("job-one")
    runner._publish_progress()
    runner._publish("state", {"from": "full_running", "to": "done", "reason": None, "source": "store"})
    from bca.api.server import ApiService

    service = ApiService(backend, backend.event_bus)
    notifications: list[dict[str, object]] = []

    async def notify(message: dict[str, object]) -> None:
        notifications.append(message)

    service.notify = notify

    async def publish() -> None:
        for event in subscription.drain():
            await service.publish_event(event)

    run(publish())
    assert {message["method"] for message in notifications} >= {"job/progress", "job/state"}


def test_unknown_job_status_is_monitor_safe(tmp_path: Path) -> None:
    assert run(make_backend(tmp_path).call("job/status", {"job": "missing"})) == {
        "job": "missing",
        "state": "unknown",
    }


def test_chat_requires_an_existing_project(tmp_path: Path) -> None:
    with pytest.raises(RpcError, match="Project not found"):
        run(make_backend(tmp_path).call("chat/send", {"project": "project-one", "text": "x"}))


def test_suspend_hint_enters_sleep_detector(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    assert run(backend.call("os/suspendHint", {})) == {"status": "accepted"}
    assert backend.sleep.sleeping is True


def test_resume_hint_returns_monitor_wake_actions(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    run(backend.call("os/suspendHint", {}))
    result = run(backend.call("os/resumeHint", {}))
    assert result["actions"] == ["requeue_in_flight", "reread_quota", "network_probe", "resume"]


def test_power_status_is_stored(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    params: dict[str, object] = {"onAc": False, "percent": 18}
    assert run(backend.call("os/powerStatus", params)) == {"status": "accepted"}
    assert backend.power_status == {"onAc": False, "percent": 18}
    assert backend._can_dispatch() is False
    run(backend.call("os/powerStatus", {"onAc": True, "percent": 18}))
    assert backend._can_dispatch() is True


def test_shutdown_requeues_and_closes_store(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    assert run(backend.call("shutdown", {})) == {"stopping": True, "exitCode": 0}
    assert backend._closed is True


def _tiny_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    latency_ms: int = 1000,
    bq_cls: type[FakeBqRunner] = FakeBqRunner,
) -> tuple[ProductionBackend, Path]:
    """A plan_proposed job over the synthetic 40-lead fixture, with the config read from site.toml."""
    write_valid_config(tmp_path / "site.toml")
    (tmp_path / ".bca-test-fakes").touch()
    monkeypatch.setenv("BCA_TEST_FAKES", "1")
    ids, preflight, calls = _synthetic_rows()
    bq = bq_cls(pages=[calls], dry=DryRun(1, [], []), preflight_pages=[preflight])
    backend = ProductionBackend(tmp_path, store=open_store(tmp_path / "store.sqlite3"), bq=bq)
    backend.store.create_project("project-one", "Synthetic")
    backend.store.create_job("job-one", "project-one", 1, JobState.plan_proposed)
    job_dir = backend.app_data / "projects" / "project-one" / "job-one"
    save_spec(
        JobSpec(
            1,
            "job-one",
            "project-one",
            "Synthetic job",
            "Synthetic question",
            LeadSourceIds("ids.txt", len(ids)),
            usable=UsableRule(min_chars=1),
            cap=CapRule(1000, "drop_short_then_oldest", 180),
            instructions="Return the answer.",
            output_schema={"type": "object", "properties": {}},
            luna_model="codex",
            luna_effort="low",
            sample_size=4,
        ),
        job_dir,
    )
    (job_dir / "ids.txt").write_text("\n".join(ids) + "\n", encoding="utf-8", newline="\n")
    (job_dir / "fake-codex-scenario.json").write_text(
        json.dumps({"latency_ms": latency_ms}), encoding="utf-8", newline="\n"
    )
    return backend, job_dir


async def _wait_state(backend: ProductionBackend, state: str, limit: float, poll: float) -> None:
    deadline = asyncio.get_running_loop().time() + limit
    current: object = None
    while asyncio.get_running_loop().time() < deadline:
        current = cast(dict[str, object], await backend.call("job/status", {"job": "job-one"})).get("state")
        if current == state:
            return
        await asyncio.sleep(poll)
    raise AssertionError(f"job did not reach {state}: {current}")


def test_polling_job_status_does_not_requeue_running_leads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend, job_dir = _tiny_job(tmp_path, monkeypatch, latency_ms=1000)

    async def scenario() -> None:
        await backend.call("job/approvePlan", {"job": "job-one", "expectedSpecVersion": 1})
        # A UI polls job/status while the sample runs; polling must not requeue or re-run anything.
        await _wait_state(backend, "sample_ready", limit=60, poll=0.1)

    run(scenario())
    assert not backend.store.rows("SELECT 1 FROM attempts WHERE error_code='engine restart'")
    sample_done = int(
        backend.store.rows(
            "SELECT COUNT(*) FROM leads WHERE job_id='job-one' AND in_sample=1 AND status='done'"
        )[0][0]
    )
    assert sample_done > 0
    turns = [
        line
        for line in (job_dir / "fake-codex-scenario.json.methods.json")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip('"') == "turn/start"
    ]
    assert len(turns) == sample_done


def test_pause_during_run_returns_fast_and_dispatches_nothing_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend, _job_dir = _tiny_job(tmp_path, monkeypatch, latency_ms=500)
    backend.concurrency = 2

    async def scenario() -> None:
        await backend.call("job/approvePlan", {"job": "job-one", "expectedSpecVersion": 1})
        await _wait_state(backend, "sample_running", limit=30, poll=0.02)
        started = time.monotonic()
        await backend.call("job/pause", {"job": "job-one"})
        elapsed = time.monotonic() - started
        assert elapsed < 2
        status = cast(dict[str, object], await backend.call("job/status", {"job": "job-one"}))
        assert status["state"] == "paused"
        reply_time = datetime.now(UTC).isoformat()
        await asyncio.sleep(0.7)
        later = backend.store.rows(
            "SELECT COUNT(*) FROM attempts WHERE job_id=? AND started_at>?", ("job-one", reply_time)
        )
        assert later[0][0] == 0

    run(scenario())


def test_full_run_after_sample_does_not_start_a_second_masker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend, _job_dir = _tiny_job(tmp_path, monkeypatch, latency_ms=1)

    async def scenario() -> None:
        await backend.call("job/approvePlan", {"job": "job-one", "expectedSpecVersion": 1})
        await _wait_state(backend, "sample_ready", limit=60, poll=0.05)
        await backend.call("job/approveSample", {"job": "job-one", "expectedSpecVersion": 1})
        await backend.call("job/startFull", {"job": "job-one"})
        for _ in range(1200):
            if backend._job_state("job-one").value in {"done", "done_with_issues"}:
                break
            await asyncio.sleep(0.05)

    run(scenario())
    held_back = backend.store.rows("SELECT status, reason FROM leads WHERE job_id=?", ("job-one",))
    final = backend._job_state("job-one").value
    # The fixture includes no-transcript leads, so done_with_issues is the correct end state.
    assert final in {"done", "done_with_issues"}, (final, [tuple(row) for row in held_back])
    assert all(str(status) != "held_back" for status, _reason in held_back)
    assert all(
        "\\" not in str(reason) and "/" not in str(reason) and "Error" not in str(reason)
        for _status, reason in held_back
    )


def test_pause_during_full_run_masker_wait_never_double_masks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_mask = mask_lead_stream
    full_started = threading.Event()
    first_full_mask_started = threading.Event()
    release_full_masking = threading.Event()
    calls: Counter[str] = Counter()
    calls_lock = threading.Lock()

    backend_ref: list[Any] = []

    def gated_mask(raw: Path, masked: Path, context: Any) -> Any:
        lead_id = raw.name.removesuffix(".jsonl.gz")
        with calls_lock:
            calls[lead_id] += 1
        in_sample = backend_ref[0].store.rows(
            "SELECT in_sample FROM leads WHERE job_id='job-one' AND lead_id=?", (lead_id,)
        )
        if in_sample and not int(in_sample[0][0]):
            # The background masker handles the non-sample leads: hold it until the
            # test has paused and resumed the full run, so the race window is certain.
            first_full_mask_started.set()
            release_full_masking.wait(30)
        return original_mask(raw, masked, context)

    monkeypatch.setattr(runner_module, "mask_lead_stream", gated_mask)
    backend, job_dir = _tiny_job(tmp_path, monkeypatch, latency_ms=1)
    backend_ref.append(backend)

    async def scenario() -> None:
        await backend.call("job/approvePlan", {"job": "job-one", "expectedSpecVersion": 1})
        await _wait_state(backend, "sample_ready", limit=60, poll=0.05)
        # Full-run leads are otherwise held back when the synthetic fixture has
        # no harvested-names file, so provide the local synthetic marker.
        (job_dir / "harvested_names.json").write_text("[]\n", encoding="utf-8", newline="\n")
        await backend.call("job/approveSample", {"job": "job-one", "expectedSpecVersion": 1})
        full_started.set()
        assert await asyncio.to_thread(first_full_mask_started.wait, 5)
        await backend.call("job/startFull", {"job": "job-one"})
        await asyncio.sleep(0.2)
        await backend.call("job/pause", {"job": "job-one"})
        # The pause reply is allowed to cancel the driver while the masking
        # thread remains blocked in gated_mask. Resume while that thread still
        # owns the masking pass, then let the real file operations proceed.
        await backend.call("job/resume", {"job": "job-one"})
        release_full_masking.set()
        deadline = asyncio.get_running_loop().time() + 90
        while asyncio.get_running_loop().time() < deadline:
            state = backend._job_state("job-one").value
            if state in {"done", "done_with_issues"}:
                return
            await asyncio.sleep(0.05)
        raise AssertionError(f"job did not finish: {backend._job_state('job-one').value}")

    run(scenario())
    rows = backend.store.rows("SELECT lead_id, status, reason FROM leads WHERE job_id=?", ("job-one",))
    assert all(str(reason) != "masking_failed" for _lead_id, _status, reason in rows)
    assert calls
    assert all(count == 1 for count in calls.values())
    # Leads without transcripts are never masked, so masked leads are a subset of all leads.
    assert set(calls) <= {str(lead_id) for lead_id, _status, _reason in rows}


def test_each_transition_emits_job_state_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend, _job_dir = _tiny_job(tmp_path, monkeypatch, latency_ms=1)
    service = ApiService(backend, backend.event_bus)
    notifications: list[dict[str, object]] = []

    async def notify(message: dict[str, object]) -> None:
        if message.get("method") == "job/state":
            notifications.append(message)

    service.notify = notify
    subscription = backend.event_bus.subscribe()

    async def scenario() -> None:
        await backend.call("job/approvePlan", {"job": "job-one", "expectedSpecVersion": 1})
        await _wait_state(backend, "sample_ready", limit=60, poll=0.05)
        for event in subscription.drain():
            await service.publish_event(event)

    run(scenario())
    transitions = int(
        backend.store.rows("SELECT COUNT(*) FROM job_events WHERE job_id=? AND kind='state'", ("job-one",))[
            0
        ][0]
    )
    assert len(notifications) == transitions
    store_states = [
        str(json.loads(str(row[0])).get("to"))
        for row in backend.store.rows(
            "SELECT data_json FROM job_events WHERE job_id=? AND kind='state' ORDER BY id", ("job-one",)
        )
    ]
    running = {
        "pulling",
        "masking_sample",
        "sample_running",
        "full_running",
        "quote_check",
        "building_output",
        "verifying",
    }
    expected = ["running" if state in running else state for state in store_states]
    states = [cast(dict[str, object], item["params"])["state"] for item in notifications]
    assert states == expected


def test_stdio_pull_failure_stalls_job_and_engine_stays_usable(tmp_path: Path) -> None:
    write_valid_config(tmp_path / "site.toml")
    backend = ProductionBackend(tmp_path, store=open_store(tmp_path / "store.sqlite3"), bq=FakeBqRunner())
    job_dir = _write_job(backend, state=JobState.plan_proposed)
    backend.store.close()
    (tmp_path / ".bca-test-fakes").touch()
    environment = os.environ.copy()
    environment.update({"BCA_DATA_DIR": str(tmp_path), "BCA_TEST_FAKES": "1"})

    def start() -> subprocess.Popen[str]:
        return subprocess.Popen(
            [sys.executable, "-m", "bca.api.stdio"],
            cwd=str(Path(__file__).parents[2]),
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def send(process: subprocess.Popen[str], request_id: int, method: str, params: dict[str, object]) -> None:
        assert process.stdin is not None
        process.stdin.write(
            json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}) + "\n"
        )
        process.stdin.flush()

    def read_response(process: subprocess.Popen[str], request_id: int) -> dict[str, object]:
        assert process.stdout is not None
        while True:
            line = process.stdout.readline()
            assert line, "engine exited"
            message = cast(dict[str, object], json.loads(line))
            if message.get("id") == request_id:
                return message

    # The pull cannot read the lead source: approval must fail cleanly and stall the job.
    (job_dir / "ids.txt").unlink(missing_ok=True)
    process = start()
    try:
        # The gate passes synchronously; the pull runs in the background and its failure stalls the job.
        send(process, 1, "job/approvePlan", {"job": "job-one", "expectedSpecVersion": 1})
        approval = read_response(process, 1)
        assert "result" in approval or cast(dict[str, object], approval["error"])["code"] == -32001
        deadline = time.monotonic() + 15
        state: object = None
        request_id = 2
        while time.monotonic() < deadline:
            send(process, request_id, "job/status", {"job": "job-one"})
            state = cast(dict[str, object], read_response(process, request_id)["result"])["state"]
            if state == "stalled":
                break
            request_id += 1
            time.sleep(0.1)
        assert state == "stalled"
        send(process, 900, "projects/list", {})
        assert read_response(process, 900)["result"] == [{"project": "project-one", "title": "Synthetic"}]
    finally:
        send(process, 901, "shutdown", {})
        read_response(process, 901)
        process.communicate(timeout=10)

    # A restarted engine on the same data dir must answer instead of crash-looping.
    second = start()
    try:
        send(second, 5, "app/status", {})
        assert read_response(second, 5)["result"]
    finally:
        send(second, 6, "shutdown", {})
        read_response(second, 6)
        second.communicate(timeout=10)


def test_approve_plan_returns_before_slow_pull(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class SlowBq(FakeBqRunner):
        def iter_pages(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
            time.sleep(3)
            yield from super().iter_pages(*args, **kwargs)

    backend, _job_dir = _tiny_job(tmp_path, monkeypatch, latency_ms=1, bq_cls=SlowBq)
    server = JsonRpcServer(ApiService(backend), request_timeout=2)

    async def scenario() -> None:
        started = time.monotonic()
        response = await server.response(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "job/approvePlan",
                "params": {"job": "job-one", "expectedSpecVersion": 1},
            }
        )
        # The pull (two slow page reads) runs in the background, not inside the request.
        assert response is not None and "result" in response, response
        assert time.monotonic() - started < 2
        await _wait_state(backend, "sample_ready", limit=45, poll=0.2)

    run(scenario())


def _stdio_job_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, delay_ms: int = 0
) -> tuple[Path, Path]:
    backend, job_dir = _tiny_job(tmp_path, monkeypatch, latency_ms=1)
    _ids, preflight, calls = _synthetic_rows()
    (tmp_path / "fake-bq.json").write_text(
        json.dumps({"pages": [calls], "preflight_pages": [preflight], "delay_ms": delay_ms}),
        encoding="utf-8",
        newline="\n",
    )
    backend.store.close()
    return job_dir, Path(str(tmp_path))


def _start_stdio(data_dir: Path) -> subprocess.Popen[str]:
    environment = os.environ.copy()
    environment.update({"BCA_DATA_DIR": str(data_dir), "BCA_TEST_FAKES": "1"})
    return subprocess.Popen(
        [sys.executable, "-m", "bca.api.stdio"],
        cwd=str(Path(__file__).parents[2]),
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _send_stdio(
    process: subprocess.Popen[str], request_id: int, method: str, params: dict[str, object]
) -> None:
    assert process.stdin is not None
    process.stdin.write(
        json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}) + "\n"
    )
    process.stdin.flush()


def _read_stdio(process: subprocess.Popen[str], request_id: int) -> dict[str, object]:
    assert process.stdout is not None
    while True:
        line = process.stdout.readline()
        assert line, "engine exited before replying"
        message = cast(dict[str, object], json.loads(line))
        if message.get("id") == request_id:
            return message


def test_engine_restart_mid_pull_resumes_and_reaches_sample_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stdio_job_fixture(tmp_path, monkeypatch, delay_ms=300)
    first = _start_stdio(tmp_path)
    try:
        _send_stdio(first, 1, "job/approvePlan", {"job": "job-one", "expectedSpecVersion": 1})
        assert "result" in _read_stdio(first, 1)
        time.sleep(0.35)
        first.kill()
        first.communicate(timeout=5)
    finally:
        if first.poll() is None:
            first.kill()
            first.communicate(timeout=5)

    second = _start_stdio(tmp_path)
    try:
        deadline = time.monotonic() + 60
        state: object = None
        request_id = 10
        while time.monotonic() < deadline:
            _send_stdio(second, request_id, "job/status", {"job": "job-one"})
            response = _read_stdio(second, request_id)
            state = cast(dict[str, object], response["result"])["state"]
            if state == "sample_ready":
                break
            request_id += 1
            time.sleep(0.1)
        assert state == "sample_ready"
    finally:
        _send_stdio(second, 99, "shutdown", {})
        _read_stdio(second, 99)
        second.communicate(timeout=5)


def test_pause_resume_mid_pull_reaches_sample_ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class SlowBq(FakeBqRunner):
        def iter_pages(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
            time.sleep(1)
            yield from super().iter_pages(*args, **kwargs)

    backend, _job_dir = _tiny_job(tmp_path, monkeypatch, bq_cls=SlowBq)

    async def scenario() -> None:
        await backend.call("job/approvePlan", {"job": "job-one", "expectedSpecVersion": 1})
        await _wait_state(backend, "pulling", limit=5, poll=0.02)
        await backend.call("job/pause", {"job": "job-one"})
        await backend.call("job/resume", {"job": "job-one"})
        await _wait_state(backend, "sample_ready", limit=60, poll=0.1)

    run(scenario())


def test_concurrent_first_requests_after_restart_all_succeed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stdio_job_fixture(tmp_path, monkeypatch)
    recover_calls = 0
    original_recover = JobManager.recover

    def counted_recover(manager: Any) -> None:
        nonlocal recover_calls
        recover_calls += 1
        original_recover(manager)

    monkeypatch.setattr(JobManager, "recover", counted_recover)
    store = open_store(tmp_path / "store.sqlite3")
    with store.transaction() as cursor:
        cursor.execute("UPDATE jobs SET state='full_running' WHERE id='job-one'")
    store.close()
    backend = ProductionBackend(tmp_path, bq=FakeBqRunner())

    async def scenario() -> tuple[object, object, object]:
        results = await asyncio.gather(
            backend.call("projects/list", {}),
            backend.call("job/status", {"job": "job-one"}),
            backend.call("settings/get", {}),
        )
        await backend.call("shutdown", {})
        return results

    results = run(scenario())
    assert len(results) == 3
    assert recover_calls == 1


def test_shutdown_stops_background_job_threads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class SlowBq(FakeBqRunner):
        def iter_pages(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
            time.sleep(2)
            yield from super().iter_pages(*args, **kwargs)

    _stdio_job_fixture(tmp_path, monkeypatch)
    store = open_store(tmp_path / "store.sqlite3")
    with store.transaction() as cursor:
        cursor.execute("UPDATE jobs SET state='full_running' WHERE id='job-one'")
    store.close()
    backend = ProductionBackend(tmp_path, bq=SlowBq())

    async def scenario() -> None:
        await backend.call("projects/list", {})
        await asyncio.sleep(0.05)
        await backend.call("shutdown", {})

    started = time.monotonic()
    run(scenario())
    assert time.monotonic() - started < 3


def test_idle_engine_cpu_is_low(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stdio_job_fixture(tmp_path, monkeypatch)
    process = _start_stdio(tmp_path)
    try:
        time.sleep(4)
        # sys.platform (not os.name) lets mypy skip the other platform's branch.
        if sys.platform != "win32":
            try:
                import psutil  # type: ignore[import-untyped]
            except ImportError:
                pytest.skip("psutil is not installed; CPU accounting is only required on Windows")
            cpu_time = sum(psutil.Process(process.pid).cpu_times()[:2])
        else:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.OpenProcess(0x1000, False, process.pid)
            creation, exit_time, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
            assert handle
            try:
                assert kernel32.GetProcessTimes(
                    handle,
                    ctypes.byref(creation),
                    ctypes.byref(exit_time),
                    ctypes.byref(kernel_time),
                    ctypes.byref(user_time),
                )
                cpu_time = (kernel_time.dwLowDateTime | kernel_time.dwHighDateTime << 32) / 10_000_000
                cpu_time += (user_time.dwLowDateTime | user_time.dwHighDateTime << 32) / 10_000_000
            finally:
                kernel32.CloseHandle(handle)
        assert cpu_time < 0.6
    finally:
        _send_stdio(process, 1, "shutdown", {})
        _read_stdio(process, 1)
        process.communicate(timeout=5)


def test_every_state_change_emits_job_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class SlowBq(FakeBqRunner):
        # Hold the pull long enough to observe the pulling state on fast machines.
        def iter_pages(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
            time.sleep(1.5)
            yield from super().iter_pages(*args, **kwargs)

    backend, _job_dir = _tiny_job(tmp_path, monkeypatch, latency_ms=1, bq_cls=SlowBq)
    service = ApiService(backend, backend.event_bus)
    notifications: list[dict[str, object]] = []

    async def notify(message: dict[str, object]) -> None:
        if message.get("method") == "job/state":
            notifications.append(message)

    service.notify = notify
    subscription = backend.event_bus.subscribe()

    async def scenario() -> None:
        await backend.call("job/approvePlan", {"job": "job-one", "expectedSpecVersion": 1})
        await _wait_state(backend, "pulling", limit=5, poll=0.02)
        await backend.call("job/pause", {"job": "job-one"})
        await backend.call("job/resume", {"job": "job-one"})
        backend.event_bus.publish(
            Event(
                "state",
                "job-one",
                time.time(),
                {"from": "pulling", "to": "stalled", "reason": "synthetic", "source": "store"},
            )
        )
        for event in subscription.drain():
            await service.publish_event(event)

    run(scenario())
    states = [cast(dict[str, object], item["params"])["state"] for item in notifications]
    assert "paused" in states
    assert "stalled" in states
    assert any(state in {"running", "sample_ready"} for state in states)


def test_chat_send_during_shutdown_is_refused_and_no_claude_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.api.test_production_chat import fake_launcher

    pid_file = tmp_path / "claude.pid"
    launcher = fake_launcher(
        tmp_path,
        "tests.fakes.fake_claude",
        {"FAKE_CLAUDE_SCENARIO": "hang", "FAKE_CLAUDE_PID_FILE": str(pid_file)},
    )
    monkeypatch.setenv("BCA_CLAUDE_BIN", str(launcher))
    monkeypatch.setenv("BCA_TEST_FAKES", "1")
    (tmp_path / ".bca-test-fakes").touch()
    backend = make_backend(tmp_path)
    backend.store.create_project("project-one", "Synthetic")

    async def scenario() -> None:
        turn = asyncio.create_task(backend.call("chat/send", {"project": "project-one", "text": "synthetic"}))
        for _ in range(200):
            if pid_file.exists():
                break
            await asyncio.sleep(0.01)
        await backend.call("shutdown", {})
        with pytest.raises(RpcError, match="stopping"):
            await backend.call("chat/send", {"project": "project-one", "text": "late"})
        await turn
        pid = int(pid_file.read_text(encoding="utf-8"))
        with pytest.raises(OSError):
            os.kill(pid, 0)

    run(scenario())


def test_stdio_shutdown_during_slow_pull_exits_quickly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stdio_job_fixture(tmp_path, monkeypatch, delay_ms=2000)
    process = _start_stdio(tmp_path)
    try:
        _send_stdio(process, 1, "job/approvePlan", {"job": "job-one", "expectedSpecVersion": 1})
        assert "result" in _read_stdio(process, 1)
        _send_stdio(process, 2, "shutdown", {})
        assert _read_stdio(process, 2)["result"] == {"stopping": True, "exitCode": 0}
        process.wait(timeout=3)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


def test_pause_mid_pull_never_stalls_and_resume_reaches_sample_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for attempt in range(3):
        data_dir = tmp_path / f"attempt-{attempt}"
        data_dir.mkdir()
        _stdio_job_fixture(data_dir, monkeypatch, delay_ms=3000)
        process = _start_stdio(data_dir)
        try:
            _send_stdio(process, 1, "job/approvePlan", {"job": "job-one", "expectedSpecVersion": 1})
            assert "result" in _read_stdio(process, 1)
            request_id = 10
            deadline = time.monotonic() + 5
            state: object = None
            while time.monotonic() < deadline:
                _send_stdio(process, request_id, "job/status", {"job": "job-one"})
                state = cast(dict[str, object], _read_stdio(process, request_id)["result"])["state"]
                if state == "pulling":
                    break
                request_id += 1
                time.sleep(0.02)
            assert state == "pulling"
            _send_stdio(process, request_id + 1, "job/pause", {"job": "job-one"})
            assert "result" in _read_stdio(process, request_id + 1)
            time.sleep(0.3)
            _send_stdio(process, request_id + 2, "job/resume", {"job": "job-one"})
            assert "result" in _read_stdio(process, request_id + 2)
            _wait_stdio_state(process, "sample_ready", time.monotonic() + 60, request_id + 3)
        finally:
            if process.poll() is None:
                _send_stdio(process, 99, "shutdown", {})
                _read_stdio(process, 99)
            process.communicate(timeout=5)


def _wait_stdio_state(
    process: subprocess.Popen[str], expected: str, deadline: float, request_id: int
) -> None:
    state: object = None
    while time.monotonic() < deadline:
        _send_stdio(process, request_id, "job/status", {"job": "job-one"})
        state = cast(dict[str, object], _read_stdio(process, request_id)["result"])["state"]
        if state == expected:
            return
        request_id += 1
        time.sleep(0.1)
    raise AssertionError(f"job did not reach {expected}: {state}")


def test_shutdown_mid_pull_then_restart_recovers_without_stall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stdio_job_fixture(tmp_path, monkeypatch, delay_ms=10_000)
    first = _start_stdio(tmp_path)
    try:
        _send_stdio(first, 1, "job/approvePlan", {"job": "job-one", "expectedSpecVersion": 1})
        assert "result" in _read_stdio(first, 1)
        time.sleep(0.5)
        first.kill()
        first.communicate(timeout=5)
    finally:
        if first.poll() is None:
            first.kill()
            first.communicate(timeout=5)

    second = _start_stdio(tmp_path)
    try:
        started = time.monotonic()
        _send_stdio(second, 2, "app/status", {})
        assert cast(dict[str, object], _read_stdio(second, 2)["result"])["state"] == "ready"
        assert time.monotonic() - started < 2
        _wait_stdio_state(second, "sample_ready", time.monotonic() + 60, 3)
    finally:
        if second.poll() is None:
            _send_stdio(second, 99, "shutdown", {})
            _read_stdio(second, 99)
        second.communicate(timeout=5)


def test_recovery_waits_for_valid_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stdio_job_fixture(tmp_path, monkeypatch, delay_ms=1)
    store = open_store(tmp_path / "store.sqlite3")
    with store.transaction() as cursor:
        cursor.execute("UPDATE jobs SET state='full_running' WHERE id='job-one'")
    store.close()
    (tmp_path / "site.toml").write_text("[bigquery]\nproject = 'broken'\n", encoding="utf-8", newline="\n")

    process = _start_stdio(tmp_path)
    try:
        _send_stdio(process, 1, "app/status", {})
        assert cast(dict[str, object], _read_stdio(process, 1)["result"])["state"] == "ready"
        # With the broken config, recovery must not start: the job stays full_running.
        time.sleep(1.0)
        check = open_store(tmp_path / "store.sqlite3")
        try:
            assert str(check.rows("SELECT state FROM jobs WHERE id='job-one'")[0][0]) == "full_running"
        finally:
            check.close()
        write_valid_config(tmp_path / "site.toml")
        _send_stdio(process, 2, "projects/list", {})
        assert "result" in _read_stdio(process, 2)
        # Recovery resumes the full run, which then finishes.
        deadline = time.monotonic() + 60
        request_id = 3
        state: object = None
        while time.monotonic() < deadline:
            _send_stdio(process, request_id, "job/status", {"job": "job-one"})
            state = cast(dict[str, object], _read_stdio(process, request_id)["result"])["state"]
            if state in {"done", "done_with_issues"}:
                break
            request_id += 1
            time.sleep(0.1)
        assert state in {"done", "done_with_issues"}
    finally:
        if process.poll() is None:
            _send_stdio(process, 99, "shutdown", {})
            _read_stdio(process, 99)
        process.communicate(timeout=5)


def test_runner_pause_emits_job_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backend, job_dir = _tiny_job(tmp_path, monkeypatch, latency_ms=1)
    (job_dir / "fake-codex-scenario.json").write_text(
        json.dumps({"used_percent": 95}), encoding="utf-8", newline="\n"
    )
    service = ApiService(backend, backend.event_bus)
    notifications: list[dict[str, object]] = []

    async def notify(message: dict[str, object]) -> None:
        if message.get("method") == "job/state":
            notifications.append(message)

    service.notify = notify
    subscription = backend.event_bus.subscribe()

    async def scenario() -> None:
        await backend.call("job/approvePlan", {"job": "job-one", "expectedSpecVersion": 1})
        await _wait_state(backend, "paused", limit=20, poll=0.05)
        for event in subscription.drain():
            await service.publish_event(event)

    run(scenario())
    assert any(cast(dict[str, object], item["params"])["state"] == "paused" for item in notifications)


def test_stdio_shutdown_during_long_pull_exits_fast(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stdio_job_fixture(tmp_path, monkeypatch, delay_ms=20_000)
    process = _start_stdio(tmp_path)
    try:
        _send_stdio(process, 1, "job/approvePlan", {"job": "job-one", "expectedSpecVersion": 1})
        assert "result" in _read_stdio(process, 1)
        _send_stdio(process, 2, "shutdown", {})
        assert _read_stdio(process, 2)["result"] == {"stopping": True, "exitCode": 0}
        process.wait(timeout=3)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


def _missing_conversation_launcher(tmp_path: Path) -> Path:
    script = tmp_path / "missing-conversation.py"
    script.write_text(
        f"#!{sys.executable}\n"
        """import json, sys
if sys.argv[1:4] == ["auth", "status", "--json"]:
    print(json.dumps({"loggedIn": True, "authMethod": "claude.ai"}), flush=True)
    raise SystemExit(0)
sys.stdin.read()
init = {
    "type": "system",
    "subtype": "init",
    "apiKeySource": "none",
    "model": "claude-opus-5-5",
    "permissionMode": "dontAsk",
    "tools": ["mcp__bq__query", "mcp__app__query", "EndConversation"],
    "mcp_servers": [{"name": "bq", "status": "connected"}, {"name": "app", "status": "connected"}],
    "plugins": [],
    "fast_mode_state": "off",
    "claude_code_version": "2.1.327",
}
if "--resume" in sys.argv:
    print("No conversation found with session ID ...", file=sys.stderr)
    raise SystemExit(1)
print(json.dumps(init), flush=True)
result = {"type": "result", "subtype": "success", "is_error": False, "modelUsage": {"claude-opus-5-5": {}}}
print(json.dumps(result), flush=True)
""",
        encoding="utf-8",
        newline="\n",
    )
    if os.name == "nt":
        launcher = tmp_path / "missing-conversation.cmd"
        launcher.write_text(f'@echo off\n"{sys.executable}" "{script}" %*\n', encoding="utf-8", newline="\n")
    else:
        launcher = script
        launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)
    return launcher


def test_stderr_missing_conversation_without_init_is_reseeded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launcher = _missing_conversation_launcher(tmp_path)
    monkeypatch.setenv("BCA_CLAUDE_BIN", str(launcher))
    monkeypatch.setenv("BCA_TEST_FAKES", "1")
    (tmp_path / ".bca-test-fakes").touch()
    backend = make_backend(tmp_path, bq=FakeBqRunner())
    backend.store.create_project("project-one", "Synthetic")
    assert run(backend.call("chat/send", {"project": "project-one", "text": "first"})) == {
        "status": "completed",
        "project": "project-one",
    }
    assert run(backend.call("chat/send", {"project": "project-one", "text": "second"})) == {
        "status": "completed",
        "project": "project-one",
    }


def test_stdin_eof_during_chat_turn_exits_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = tmp_path / "claude.pid"
    process = _start(tmp_path, "hang", FAKE_CLAUDE_PID_FILE=str(pid_file))
    try:
        _create_project(process)
        _send(process, 2, "chat/send", {"project": "project-one", "text": "synthetic"})
        deadline = time.monotonic() + 5
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pid_file.exists()
        assert process.stdin is not None
        process.stdin.close()
        process.wait(timeout=5)
        pid = int(pid_file.read_text(encoding="utf-8"))
        with pytest.raises(OSError):
            os.kill(pid, 0)
        assert not (tmp_path / "projects" / "project-one" / "chat" / "mcp.json").exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


def test_stdio_shutdown_during_hanging_chat_turn_replies_fast_and_cleans_up(
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "claude.pid"
    process = _start(tmp_path, "hang", FAKE_CLAUDE_PID_FILE=str(pid_file))
    try:
        _create_project(process)
        _send(process, 2, "chat/send", {"project": "project-one", "text": "synthetic"})
        deadline = time.monotonic() + 5
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pid_file.exists()

        _send(process, 3, "shutdown", {})
        started = time.monotonic()
        response = _read_stdio(process, 3)
        assert time.monotonic() - started < 3
        assert response["result"] == {"stopping": True, "exitCode": 0}
        process.wait(timeout=5)

        pid = int(pid_file.read_text(encoding="utf-8"))
        with pytest.raises(OSError):
            os.kill(pid, 0)
        assert not (tmp_path / "projects" / "project-one" / "chat" / "mcp.json").exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
