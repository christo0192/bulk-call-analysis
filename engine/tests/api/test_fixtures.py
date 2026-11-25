from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any

import pytest

from tests.api.t04_lifecycle import captured_notifications, run_lifecycle
from tests.api.test_production import make_backend
from tests.api.test_production_chat import allow_fakes
from tests.fakes.fake_bq import FakeBqRunner

FIXTURE_DIR = Path(__file__).parents[3] / "docs" / "api-fixtures"
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b", re.IGNORECASE)


_TIMING_KEYS = frozenset({"throughputPerMin", "low", "high", "durationSeconds", "asleepSeconds", "elapsed"})


def _normalise(value: Any, data_dir: Path, ids: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {
            key: 1700000000
            if isinstance(item, (int, float)) and (key.endswith("At") or key.endswith("Ts") or key == "ts")
            # Measured timings vary from run to run; keep the field and its type, not the value.
            else (1.0 if isinstance(item, float) else 1)
            if isinstance(item, (int, float)) and not isinstance(item, bool) and key in _TIMING_KEYS
            else _normalise(item, data_dir, ids)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_normalise(item, data_dir, ids) for item in value]
    if isinstance(value, str):
        text = value.replace(str(data_dir), "<dataDir>")
        match = _UUID.fullmatch(text)
        if match:
            return ids.setdefault(text, f"uuid-{len(ids) + 1}")
        if text.startswith("attempt-"):
            return "attempt-1"
        return text
    return value


def _assert_progress_identity(snapshot: object) -> None:
    assert isinstance(snapshot, dict)
    progress = snapshot.get("progress")
    assert isinstance(progress, dict)
    fields = ("done", "inFlight", "retrying", "failed", "heldBack", "remaining")
    assert all(isinstance(progress.get(field), int) for field in fields)
    assert isinstance(progress.get("total"), int)
    assert sum(int(progress[field]) for field in fields) == progress["total"]


async def _chat(tmp_path: Path) -> tuple[list[dict[str, object]], object]:
    """One real chat turn against the fake Claude: its notifications and the stored history."""
    backend = make_backend(tmp_path, bq=FakeBqRunner(pages=[[{"note": "synthetic row"}]]))
    await backend.call("projects/create", {"project": "project-one", "title": "Synthetic"})
    async with captured_notifications(backend) as sent:
        await backend.call("chat/send", {"project": "project-one", "text": "How many leads are there?"})
    return sent, await backend.call("chat/history", {"project": "project-one"})


def _kind(message: dict[str, Any]) -> str:
    params = message["params"]
    event = params.get("event")
    if isinstance(event, dict):
        return str(event.get("kind"))
    if message["method"] == "job/state":
        return str(params.get("step"))
    if message["method"] == "job/progress":
        return str(params.get("phase"))
    kind = str(params.get("kind"))
    return f"{kind}:{params.get('reason')}" if kind == "pause" else kind


def _one_of_each(messages: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """The first message of every kind per method: timing decides how many arrive, not which."""
    chosen: dict[tuple[str, str], dict[str, Any]] = {}
    for message in messages:
        chosen.setdefault((str(message["method"]), _kind(message)), message)
    result: dict[str, list[dict[str, Any]]] = {
        method: [] for method in ("job/state", "job/progress", "chat/event", "engine/event")
    }
    for (method, _), message in sorted(chosen.items()):
        result.setdefault(method, []).append(message)
    return result


def _shape(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _shape(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return [_shape(value[0])] if value else []
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return type(value).__name__
    return "number"


REQUIRED_KINDS = {
    "job/state": {"sample_running", "sample_ready", "sample_approved", "full_running", "paused"},
    "job/progress": {"sample", "full"},
    "engine/event": {"pause:user", "pause:quota", "counts", "state"},
    "chat/event": {"system", "tool_use", "tool_result", "result"},
}


async def _responses(tmp_path: Path) -> dict[str, object]:
    lifecycle = await run_lifecycle(tmp_path)
    chat_sent, chat_history = await _chat(tmp_path / "chat")
    backend = lifecycle.backend
    _assert_progress_identity(lifecycle.running)
    _assert_progress_identity(lifecycle.paused)
    return {
        "job-plan.json": lifecycle.plan,
        "job-sample.json": lifecycle.sample,
        "job-snapshot-running.json": lifecycle.running,
        "job-snapshot-paused.json": lifecycle.paused,
        "job-snapshot-paused-quota.json": lifecycle.paused_quota,
        "job-outputs.json": lifecycle.outputs,
        "projects-list.json": await backend.call("projects/list", {}),
        "projects-jobs.json": await backend.call("projects/jobs", {"project": "project-one"}),
        "settings-get.json": await backend.call("settings/get", {}),
        "setup-checklist.json": await backend.call("setup/checklist", {}),
        "chat-history.json": chat_history,
        "allowance-status.json": await backend.call("allowance/status", {}),
        "notifications.json": _one_of_each(lifecycle.notifications + chat_sent),
    }


def test_api_fixtures_are_recordings_of_the_real_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "chat").mkdir()
    allow_fakes(monkeypatch, tmp_path / "chat", {"FAKE_CLAUDE_SCENARIO": "tool_call"})
    values = asyncio.run(_responses(tmp_path))
    ids: dict[str, str] = {}
    normalised = {name: _normalise(value, tmp_path, ids) for name, value in values.items()}
    if os.environ.get("BCA_UPDATE_FIXTURES") == "1":
        for name, value in normalised.items():
            (FIXTURE_DIR / name).write_text(
                json.dumps(value, separators=(",", ":")) + "\n", encoding="utf-8", newline="\n"
            )
    recorded = json.loads((FIXTURE_DIR / "notifications.json").read_text(encoding="utf-8"))
    sent = normalised.pop("notifications.json")
    assert isinstance(sent, dict)
    for method, kinds in REQUIRED_KINDS.items():
        old = {_kind(message): message for message in recorded[method]}
        new = {_kind(message): message for message in sent[method]}
        assert kinds <= old.keys(), f"{method}: the fixture has no {kinds - old.keys()}"
        assert kinds <= new.keys(), f"{method}: the engine no longer sends {kinds - new.keys()}"
        for kind in kinds:
            # Counts and times differ from run to run; the fields and their types must not.
            assert _shape(old[kind]) == _shape(new[kind]), f"{method} {kind}"
    for name, value in normalised.items():
        assert json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8")) == value, name


def test_api_fixtures_are_individual_strict_json_documents() -> None:
    files = sorted(FIXTURE_DIR.glob("*.json"))
    assert files
    assert not (FIXTURE_DIR / "read-model-contract.json").exists()
    for path in files:
        value = json.loads(path.read_text(encoding="utf-8"))
        assert value is not None
        assert "C:\\" not in path.read_text(encoding="utf-8")


def test_api_fixtures_contain_only_synthetic_identifiers() -> None:
    text = "\n".join(path.read_text(encoding="utf-8") for path in FIXTURE_DIR.glob("*.json"))
    assert "9876543210" not in text
    assert "@" not in text
