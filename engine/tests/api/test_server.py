from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import sys
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, TypeVar, cast

import pytest

from bca.api.server import ApiBackend, ApiService, JsonRpcServer
from bca.monitor.events import Event, EventBus

T = TypeVar("T")


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def error_code(response: dict[str, object]) -> int:
    error = cast(dict[str, object], response["error"])
    return cast(int, error["code"])


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("app/status", {}),
        ("setup/checklist", {}),
        ("setup/runChecks", {}),
        ("settings/get", {}),
        ("projects/list", {}),
        ("auth/status", {}),
        ("os/suspendHint", {}),
        ("os/resumeHint", {}),
        ("projects/create", {"project": "p", "title": "Project"}),
        ("files/list", {"project": "p"}),
        ("setup/markManual", {"item": "training_off"}),
        ("chat/send", {"project": "p", "text": "hello"}),
        ("chat/stop", {"project": "p"}),
        ("job/status", {"job": "j"}),
        ("job/approvePlan", {"job": "j", "expectedSpecVersion": 1}),
        ("job/approveSample", {"job": "j", "expectedSpecVersion": 1}),
        ("job/startFull", {"job": "j"}),
        ("job/pause", {"job": "j"}),
        ("job/resume", {"job": "j"}),
        ("job/rebuildRules", {"job": "j", "rules": []}),
        ("auth/claudeLogin", {}),
        ("auth/codexLogin", {}),
    ],
)
def test_method_happy_path(method: str, params: dict[str, object]) -> None:
    response = run(
        JsonRpcServer(ApiService()).response({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    )
    assert isinstance(response, dict)
    assert "result" in response


def test_unknown_method_and_invalid_params() -> None:
    server = JsonRpcServer(ApiService())
    unknown = run(server.response({"jsonrpc": "2.0", "id": 1, "method": "no/such", "params": {}}))
    bad = run(server.response({"jsonrpc": "2.0", "id": 2, "method": "files/list", "params": {}}))
    assert isinstance(unknown, dict) and error_code(unknown) == -32601
    assert isinstance(bad, dict) and error_code(bad) == -32602


def test_model_change_requires_explicit_confirmation() -> None:
    server = JsonRpcServer(ApiService())
    response = run(
        server.response(
            {"jsonrpc": "2.0", "id": 1, "method": "settings/set", "params": {"claudeModel": "claude-x"}}
        )
    )
    assert isinstance(response, dict) and error_code(response) == -32602


def test_chat_notification_is_delivered() -> None:
    sent: list[dict[str, object]] = []

    async def notify(value: dict[str, object]) -> None:
        sent.append(value)

    async def exercise() -> None:
        service = ApiService()
        service.notify = notify
        response = await service.dispatch("chat/send", {"project": "p", "text": "hello"})
        assert response == {"status": "accepted"}

    run(exercise())
    assert sent[0]["method"] == "chat/event"


def test_monitor_event_bus_notification() -> None:
    sent: list[dict[str, object]] = []

    async def notify(value: dict[str, object]) -> None:
        sent.append(value)

    async def exercise() -> None:
        bus = EventBus()
        service = ApiService(event_bus=bus)
        service.notify = notify
        subscription = bus.subscribe()
        bus.publish(Event("notice", "job-1", 1.0, {"message": "ready"}))
        await service.publish_event(subscription.drain()[0])

    run(exercise())
    assert sent[0]["method"] == "engine/event"


def test_stdio_engine_forwards_job_state_and_progress() -> None:
    sent: list[dict[str, object]] = []

    async def notify(value: dict[str, object]) -> None:
        sent.append(value)

    async def exercise() -> None:
        service = ApiService(event_bus=EventBus())
        service.notify = notify
        await service.publish_event(
            Event(
                "state",
                "job-1",
                1.0,
                {"from": "draft", "to": "running", "project": "project-one", "source": "store"},
            )
        )
        await service.publish_event(
            Event(
                "counts",
                "job-1",
                1.0,
                {"done": 2, "remaining": 3, "in_flight": 1, "failed": 0, "held_back": 0},
            )
        )

    run(exercise())
    state = next(item for item in sent if item["method"] == "job/state")
    progress = next(item for item in sent if item["method"] == "job/progress")
    assert set(cast(dict[str, object], state["params"])) == {"job", "project", "state", "step"}
    assert set(cast(dict[str, object], progress["params"])) == {
        "job",
        "phase",
        "done",
        "inFlight",
        "retrying",
        "failed",
        "heldBack",
        "remaining",
        "total",
    }


class SlowBackend(ApiBackend):
    async def call(self, method: str, params: dict[str, object]) -> object:
        await asyncio.sleep(0.05)
        return None


def test_request_timeout() -> None:
    server = JsonRpcServer(ApiService(SlowBackend()), request_timeout=0.001)
    response = run(server.response({"jsonrpc": "2.0", "id": 1, "method": "projects/list", "params": {}}))
    assert isinstance(response, dict) and error_code(response) == -32000


def test_malformed_line_and_shutdown_through_stdio() -> None:
    incoming = io.StringIO(
        "not json\n" + json.dumps({"jsonrpc": "2.0", "id": 1, "method": "shutdown", "params": {}}) + "\n"
    )
    outgoing = io.StringIO()
    asyncio.run(JsonRpcServer(ApiService()).serve(incoming, outgoing))
    messages = [json.loads(line) for line in outgoing.getvalue().splitlines()]
    assert messages[0]["error"]["code"] == -32700
    assert messages[1]["result"] == {"stopping": True}


def test_stdio_subprocess_tiny_job_protocol() -> None:
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "job/approvePlan",
            "params": {"job": "job-1", "expectedSpecVersion": 1},
        },
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "job/approveSample",
            "params": {"job": "job-1", "expectedSpecVersion": 1},
        },
        {"jsonrpc": "2.0", "id": 3, "method": "job/startFull", "params": {"job": "job-1"}},
        {"jsonrpc": "2.0", "id": 4, "method": "job/status", "params": {"job": "job-1"}},
        {"jsonrpc": "2.0", "id": 5, "method": "shutdown", "params": {}},
    ]
    environment = os.environ.copy()
    process = subprocess.Popen(
        [sys.executable, "-m", "tests.api.memory_stdio"],
        cwd=str(Path(__file__).parents[2]),
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    input_text = "".join(json.dumps(request) + "\n" for request in requests)
    stdout, stderr = process.communicate(input_text, timeout=10)
    assert process.returncode == 0, stderr
    responses = [json.loads(line) for line in stdout.splitlines()]
    assert [response["id"] for response in responses] == [1, 2, 3, 4, 5]
    assert responses[3]["result"]["state"] == "running"
