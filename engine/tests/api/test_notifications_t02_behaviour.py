from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from typing import Any, cast

import pytest

from bca.api.server import ApiBackend, ApiService, JsonRpcServer, RpcError
from bca.monitor.events import Event, EventBus
from bca.store import JobState, transition
from tests.api.test_production import _tiny_job, _wait_state, _write_job, make_backend, run
from tests.api.test_production_chat import allow_fakes
from tests.coordinator.conftest import FakeClock


def test_server_writes_only_strict_json_for_every_notification_kind() -> None:
    class Backend(ApiBackend):
        async def call(self, method: str, params: dict[str, object]) -> object:
            if method == "projects/list":
                for kind, data in (
                    (
                        "state",
                        {"project": "project-one", "from": "draft", "to": "full_running", "source": "store"},
                    ),
                    (
                        "counts",
                        {
                            "project": "project-one",
                            "done": 1,
                            "in_flight": 0,
                            "retrying": 0,
                            "failed": 0,
                            "held_back": 0,
                            "remaining": 1,
                            "total": 2,
                        },
                    ),
                    ("eta", {"low": 1.0, "high": 2.0, "known": True}),
                    ("eta", {"low": float("inf"), "high": float("nan"), "known": False}),
                    ("activity", {"last_activity_ts": float("inf"), "workers": 1}),
                    ("pause", {"reason": "user"}),
                ):
                    bus.publish(Event(cast(Any, kind), "job-one", 1.0, cast(dict[str, object], data)))
                await asyncio.sleep(0)
            if method == "chat/send":
                await asyncio.sleep(0)
                return {"kind": "result", "data": {"usage": float("nan")}}
            return []

    bus = EventBus()
    backend = Backend()
    service = ApiService(backend, bus)
    outgoing = io.StringIO()
    requests = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "projects/list", "params": {}})
        + "\n"
        + json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "chat/send",
                "params": {"project": "project-one", "text": "synthetic"},
            }
        )
        + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 3, "method": "shutdown", "params": {}})
        + "\n"
    )
    run(JsonRpcServer(service).serve(requests, outgoing))
    for line in outgoing.getvalue().splitlines():
        json.loads(line, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))


def test_job_state_params_include_project_and_step(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    _write_job(backend, state=JobState.plan_approved)
    sent: list[dict[str, object]] = []

    async def scenario() -> None:
        service = ApiService(backend, backend.event_bus)

        async def notify(message: dict[str, object]) -> None:
            sent.append(message)

        service.notify = notify
        subscription = backend.event_bus.subscribe()
        transition(backend.store, "job-one", JobState.pulling, "synthetic")
        for event in subscription.drain():
            await service.publish_event(event)

    run(scenario())
    state = next(item for item in sent if item["method"] == "job/state")
    assert state["params"] == {
        "job": "job-one",
        "project": "project-one",
        "state": "running",
        "step": "pulling",
    }


def test_job_status_returns_public_state_and_internal_step(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    _write_job(backend, state=JobState.full_running)
    assert run(backend.call("job/status", {"job": "job-one"}))["state"] == "running"
    assert run(backend.call("job/status", {"job": "job-one"}))["step"] == "full_running"


def test_sample_approved_is_its_own_public_state(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    _write_job(backend, state=JobState.sample_approved)
    status = cast(dict[str, object], run(backend.call("job/status", {"job": "job-one"})))
    assert status["state"] == "sample_approved"
    assert status["step"] == "sample_approved"


def test_job_progress_identity_holds_through_the_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backend, _ = _tiny_job(tmp_path, monkeypatch, latency_ms=1)
    sent: list[dict[str, object]] = []

    async def scenario() -> None:
        service = ApiService(backend, backend.event_bus)

        async def notify(message: dict[str, object]) -> None:
            if message["method"] == "job/progress":
                sent.append(message)

        service.notify = notify
        subscription = backend.event_bus.subscribe()
        await backend.call("job/approvePlan", {"job": "job-one", "expectedSpecVersion": 1})
        await _wait_state(backend, "sample_ready", 60, 0.02)
        for event in subscription.drain():
            await service.publish_event(event)

    run(scenario())
    assert sent
    fields = ("done", "inFlight", "retrying", "failed", "heldBack", "remaining", "total")
    for message in sent:
        params = cast(dict[str, object], message["params"])
        assert all(isinstance(params[field], int) and not isinstance(params[field], bool) for field in fields)
        values = [cast(int, params[field]) for field in fields]
        assert sum(values[:-1]) == values[-1]


def test_heartbeat_publishes_counts_eta_and_activity_while_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend, _ = _tiny_job(tmp_path, monkeypatch, latency_ms=60000)
    clock = FakeClock()
    backend.clock = clock
    runner = backend.jobs.runner("job-one")
    events: list[Event] = []
    subscription = backend.event_bus.subscribe()
    original_sleep = asyncio.sleep

    async def fast_sleep(delay: float) -> None:
        if delay == 10:
            clock.advance(10)
            await original_sleep(0)
        else:
            await original_sleep(delay)

    monkeypatch.setattr("bca.coordinator.runner.asyncio.sleep", fast_sleep)

    async def scenario() -> None:
        task = asyncio.create_task(runner._heartbeat(), name="bca-heartbeat-test")
        runner._phase_running = True
        # Give the heartbeat enough scheduling turns to run at least two 10 s ticks of fake time.
        for _ in range(50):
            await asyncio.sleep(0)
            events.extend(subscription.drain())
            if len([event for event in events if event.kind == "activity"]) >= 2:
                break
        runner._phase_running = False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    run(scenario())
    kinds = {event.kind for event in events}
    assert {"counts", "eta", "activity"} <= kinds
    assert len([event for event in events if event.kind == "activity"]) >= 2
    # No lead finished: the heartbeat, not a completion, produced these events.
    assert not backend.store.rows("SELECT 1 FROM results WHERE job_id=?", ("job-one",))


def test_heartbeat_stops_on_pause_and_on_driver_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backend, _ = _tiny_job(tmp_path, monkeypatch, latency_ms=500)
    runner = backend.jobs.runner("job-one")

    async def scenario() -> None:
        await backend.call("job/approvePlan", {"job": "job-one", "expectedSpecVersion": 1})
        await _wait_state(backend, "sample_running", 30, 0.02)
        await backend.call("job/pause", {"job": "job-one"})
        assert runner._heartbeat_task is None
        runner.request_stop()
        await backend.jobs.stop("job-one")
        assert runner._heartbeat_task is None

    run(scenario())


def test_activity_event_reports_last_activity_and_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend, _ = _tiny_job(tmp_path, monkeypatch, latency_ms=1)
    runner = backend.jobs.runner("job-one")
    subscription = backend.event_bus.subscribe()
    runner._last_activity_ts = 42.0
    runner._phase_running = True
    runner._publish("activity", {"last_activity_ts": runner._last_activity_ts, "in_flight": 2, "workers": 4})
    event = subscription.drain()[0]
    sent: list[dict[str, object]] = []

    async def scenario() -> None:
        service = ApiService(backend, backend.event_bus)

        async def notify(message: dict[str, object]) -> None:
            sent.append(message)

        service.notify = notify
        await service.publish_event(event)

    run(scenario())
    assert sent[0]["method"] == "engine/event"
    assert cast(dict[str, object], sent[0]["params"])["workers"] == 4
    assert cast(dict[str, object], sent[0]["params"])["last_activity_ts"] == 42.0


def test_chat_send_reports_claude_extra_usage_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    allow_fakes(monkeypatch, tmp_path, {"FAKE_CLAUDE_SCENARIO": "extra_usage"})
    backend = make_backend(tmp_path)
    # The project must exist; otherwise chat/send fails earlier with "Project not found".
    run(backend.call("projects/create", {"project": "project-one", "title": "Synthetic"}))
    with pytest.raises(RpcError) as error:
        run(backend.call("chat/send", {"project": "project-one", "text": "synthetic"}))
    assert error.value.code == -32001
    assert cast(dict[str, object], error.value.data) == {
        "project": "project-one",
        "reason": "claude_extra_usage_on",
        "message": "Claude extra usage is enabled",
    }


def test_chat_send_reports_claude_limit_reached_with_resets_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    allow_fakes(monkeypatch, tmp_path, {"FAKE_CLAUDE_SCENARIO": "rate_limit"})
    backend = make_backend(tmp_path)
    # The project must exist; otherwise chat/send fails earlier with "Project not found".
    run(backend.call("projects/create", {"project": "project-one", "title": "Synthetic"}))
    with pytest.raises(RpcError) as error:
        run(backend.call("chat/send", {"project": "project-one", "text": "synthetic"}))
    data = cast(dict[str, object], error.value.data)
    assert data["project"] == "project-one"
    assert data["reason"] == "claude_limit_reached"
    assert isinstance(data["resetsAt"], int)
