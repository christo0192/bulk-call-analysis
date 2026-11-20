from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import cast

import pytest

from tests.api.test_production_chat import fake_launcher


def _start(tmp_path: Path, scenario: str, **extra: str) -> subprocess.Popen[str]:
    config = tmp_path / "site.toml"
    config.write_text(
        "\n".join(
            [
                "[bigquery]",
                "project = 'synthetic-project'",
                "[recipe]",
                "transcripts_table = 'dataset.transcripts'",
                "calls_table = 'dataset.calls'",
                "lead_id_column = 'lead_id'",
                "call_id_column = 'call_id'",
                "transcript_text_column = 'text'",
                "call_start_column = 'started'",
                "call_duration_column = 'duration'",
                "[models]",
                "[limits]",
                "[retention]",
                "[masking]",
                "",
            ]
        ),
        encoding="utf-8",
        newline="\n",
    )
    # The guarded test-fakes switch needs BOTH the env flag and this marker in the data dir.
    (tmp_path / ".bca-test-fakes").touch()
    env = os.environ.copy()
    env.update(
        {
            "BCA_DATA_DIR": str(tmp_path),
            "BCA_TEST_FAKES": "1",
            "BCA_CLAUDE_BIN": str(
                fake_launcher(
                    tmp_path, "tests.fakes.fake_claude", {"FAKE_CLAUDE_SCENARIO": scenario, **extra}
                )
            ),
        }
    )
    return subprocess.Popen(
        [sys.executable, "-m", "bca.api.stdio"],
        cwd=str(Path(__file__).parents[2]),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _send(process: subprocess.Popen[str], request_id: int, method: str, params: dict[str, object]) -> None:
    assert process.stdin is not None
    process.stdin.write(
        json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}) + "\n"
    )
    process.stdin.flush()


def _read(process: subprocess.Popen[str]) -> dict[str, object]:
    assert process.stdout is not None
    line = process.stdout.readline()
    assert line, process.stderr.read() if process.stderr is not None else "stdio child exited"
    return cast(dict[str, object], json.loads(line))


def _read_response(process: subprocess.Popen[str], request_id: int) -> dict[str, object]:
    while True:
        message = _read(process)
        if message.get("id") == request_id:
            return message


def _create_project(process: subprocess.Popen[str]) -> None:
    _send(process, 1, "projects/create", {"project": "project-one", "title": "Synthetic"})
    assert _read(process)["id"] == 1


def _shutdown(process: subprocess.Popen[str]) -> None:
    _send(process, 99, "shutdown", {})
    assert _read_response(process, 99)["id"] == 99
    process.communicate(timeout=10)


def test_stdio_chat_stop_stops_a_hanging_turn_within_seconds(tmp_path: Path) -> None:
    process = _start(tmp_path, "hang")
    try:
        _create_project(process)
        _send(process, 2, "chat/send", {"project": "project-one", "text": "synthetic"})
        time.sleep(0.2)
        _send(process, 3, "chat/stop", {"project": "project-one"})
        responses: dict[object, dict[str, object]] = {}
        while {2, 3} - responses.keys():
            response = _read(process)
            if "id" in response:
                responses[response["id"]] = response
        assert responses[2]["result"] == {"status": "stopped", "project": "project-one"}
        assert responses[3]["result"] == {"status": "stopped", "project": "project-one"}
    finally:
        _shutdown(process)


def test_stdio_turn_longer_than_30s_completes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BCA_API_REQUEST_TIMEOUT", "2")
    process = _start(tmp_path, "normal", FAKE_CLAUDE_DELAY="4")
    try:
        _create_project(process)
        _send(process, 2, "chat/send", {"project": "project-one", "text": "synthetic"})
        # chat/event notifications stream before the reply: read until the response with id 2.
        response = _read_response(process, 2)
        assert response["result"] == {"status": "completed", "project": "project-one"}
    finally:
        _shutdown(process)


def test_stdio_other_requests_answered_during_a_turn(tmp_path: Path) -> None:
    process = _start(tmp_path, "hang")
    try:
        _create_project(process)
        _send(process, 2, "chat/send", {"project": "project-one", "text": "synthetic"})
        _send(process, 3, "app/status", {})
        _send(process, 4, "health/ping", {})
        seen: dict[object, dict[str, object]] = {}
        while {3, 4} - seen.keys():
            response = _read(process)
            if "id" in response:
                seen[response["id"]] = response
        assert cast(dict[str, object], seen[3]["result"])["state"] == "ready"
        assert seen[4]["result"] == {"status": "ok"}
        _send(process, 5, "chat/stop", {"project": "project-one"})
        seen_ids: set[object] = set()
        while {2, 5} - seen_ids:
            response = _read(process)
            if "id" in response:
                seen_ids.add(response["id"])
    finally:
        _shutdown(process)
