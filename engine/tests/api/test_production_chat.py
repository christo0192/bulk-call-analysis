from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import sys
import time
from itertools import count
from pathlib import Path
from typing import Any, cast

import pytest

import bca.api.production as production
from bca.api.production import ProductionBackend
from bca.api.server import RpcError
from bca.claude.auth import AuthStatus
from bca.config import (
    BigQueryConfig,
    LimitsConfig,
    MaskingConfig,
    ModelsConfig,
    RecipeConfig,
    RetentionConfig,
    SiteConfig,
)
from bca.procutil import pid_alive
from bca.store import open_store
from tests.fakes.fake_bq import FakeBqRunner

_launcher_counter = count()


def run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


def make_backend(tmp_path: Path, **kwargs: Any) -> ProductionBackend:
    config = SiteConfig(
        BigQueryConfig("synthetic-project"),
        RecipeConfig(
            "dataset.transcripts", "dataset.calls", "lead_id", "call_id", "text", "started", "duration"
        ),
        ModelsConfig(),
        LimitsConfig(),
        RetentionConfig(),
        MaskingConfig(),
    )
    backend = ProductionBackend(tmp_path, config=config, store=open_store(Path(":memory:")), **kwargs)
    backend.store.create_project("project-one", "Synthetic")
    backend.store.create_project("project-two", "Synthetic")
    return backend


def fake_launcher(tmp_path: Path, module: str, env: dict[str, str] | None = None) -> Path:
    fake_path = Path(__file__).parents[2].joinpath(*module.split(".")).with_suffix(".py")
    variables = env or {}
    launcher_id = next(_launcher_counter)
    if os.name == "nt":
        path = tmp_path / f"{module.rsplit('.', 1)[-1]}-{launcher_id}.cmd"
        assignments = "".join(f'set "{key}={value}"\n' for key, value in variables.items())
        path.write_text(
            f'@echo off\n{assignments}"{sys.executable}" "{fake_path}" %*\n',
            encoding="utf-8",
            newline="\n",
        )
    else:
        path = tmp_path / f"{module.rsplit('.', 1)[-1]}-{launcher_id}"
        assignments = "".join(f"export {key}={value!r}\n" for key, value in variables.items())
        path.write_text(
            f"#!/bin/sh\n{assignments}exec '{sys.executable}' '{fake_path}' \"$@\"\n",
            encoding="utf-8",
            newline="\n",
        )
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def allow_fakes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, env: dict[str, str] | None = None) -> None:
    (tmp_path / ".bca-test-fakes").touch()
    monkeypatch.setenv("BCA_TEST_FAKES", "1")
    monkeypatch.setenv("BCA_CLAUDE_BIN", str(fake_launcher(tmp_path, "tests.fakes.fake_claude", env)))


def test_chat_turn_streams_events_and_tool_call_reaches_toolhost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    allow_fakes(monkeypatch, tmp_path, {"FAKE_CLAUDE_SCENARIO": "tool_call"})
    bq = FakeBqRunner(pages=[[{"phone": "Call 9876543210"}]])
    backend = make_backend(tmp_path, bq=bq)
    events: list[dict[str, object]] = []
    backend.set_chat_notify(lambda _project, event: _record(events, event))

    result = run(backend.call("chat/send", {"project": "project-one", "text": "call-one-synthetic"}))

    assert result == {"status": "completed", "project": "project-one"}
    assert [event["kind"] for event in events] == ["system", "tool_use", "tool_result", "result"]
    assert "[PHONE]" in str(events[2])
    assert bq.calls


async def _record(events: list[dict[str, object]], event: dict[str, object]) -> None:
    events.append(event)


def test_chat_stop_mid_turn_keeps_toolhost_healthy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    allow_fakes(monkeypatch, tmp_path, {"FAKE_CLAUDE_SCENARIO": "spawn_child"})
    child_pid_file = tmp_path / "child.pid"
    monkeypatch.setenv("FAKE_CLAUDE_CHILD_PID", str(child_pid_file))
    monkeypatch.setenv(
        "BCA_CLAUDE_BIN",
        str(
            fake_launcher(
                tmp_path,
                "tests.fakes.fake_claude",
                {"FAKE_CLAUDE_SCENARIO": "spawn_child", "FAKE_CLAUDE_CHILD_PID": str(child_pid_file)},
            )
        ),
    )
    backend = make_backend(tmp_path, bq=FakeBqRunner())

    async def scenario() -> None:
        turn = asyncio.create_task(backend.call("chat/send", {"project": "project-one", "text": "x"}))
        for _ in range(100):
            if child_pid_file.exists():
                break
            await asyncio.sleep(0.01)
        assert child_pid_file.exists()
        await backend.call("chat/stop", {"project": "project-one"})
        child_pid = int(child_pid_file.read_text(encoding="utf-8"))
        for _ in range(100):
            if not pid_alive(child_pid):
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("chat/stop left the fake Claude child process alive")
        assert await turn == {"status": "stopped", "project": "project-one"}
        assert not backend._chat_tasks
        monkeypatch.setenv(
            "BCA_CLAUDE_BIN",
            str(fake_launcher(tmp_path, "tests.fakes.fake_claude", {"FAKE_CLAUDE_SCENARIO": "normal"})),
        )
        assert await backend.call("chat/send", {"project": "project-one", "text": "x"}) == {
            "status": "completed",
            "project": "project-one",
        }

    run(scenario())


def test_new_turn_after_stop_is_not_reported_stopped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    allow_fakes(monkeypatch, tmp_path, {"FAKE_CLAUDE_SCENARIO": "hang"})
    pid_file = tmp_path / "hanging.pid"
    monkeypatch.setenv("BCA_CLAUDE_PID_FILE", str(pid_file))
    monkeypatch.setenv(
        "BCA_CLAUDE_BIN",
        str(
            fake_launcher(
                tmp_path,
                "tests.fakes.fake_claude",
                {"FAKE_CLAUDE_SCENARIO": "hang", "FAKE_CLAUDE_PID_FILE": str(pid_file)},
            )
        ),
    )
    backend = make_backend(tmp_path, bq=FakeBqRunner())

    async def scenario() -> None:
        turn = asyncio.create_task(backend.call("chat/send", {"project": "project-one", "text": "x"}))
        await asyncio.sleep(0.1)
        await backend.call("chat/stop", {"project": "project-one"})
        assert await turn == {"status": "stopped", "project": "project-one"}

        monkeypatch.setenv(
            "BCA_CLAUDE_BIN",
            str(fake_launcher(tmp_path, "tests.fakes.fake_claude", {"FAKE_CLAUDE_SCENARIO": "normal"})),
        )
        assert await backend.call("chat/send", {"project": "project-one", "text": "x"}) == {
            "status": "completed",
            "project": "project-one",
        }

    run(scenario())


def test_second_concurrent_turn_same_project_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    allow_fakes(monkeypatch, tmp_path, {"FAKE_CLAUDE_SCENARIO": "hang"})
    pid_file = tmp_path / "hanging.pid"
    monkeypatch.setenv("FAKE_CLAUDE_PID_FILE", str(pid_file))
    monkeypatch.setenv(
        "BCA_CLAUDE_BIN",
        str(
            fake_launcher(
                tmp_path,
                "tests.fakes.fake_claude",
                {"FAKE_CLAUDE_SCENARIO": "hang", "FAKE_CLAUDE_PID_FILE": str(pid_file)},
            )
        ),
    )
    backend = make_backend(tmp_path, bq=FakeBqRunner())

    async def scenario() -> None:
        first = asyncio.create_task(
            backend.call("chat/send", {"project": "project-one", "text": "call-one-synthetic"})
        )
        for _ in range(1000):
            if pid_file.exists():
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("the hanging fake Claude process did not start within 10 seconds")
        with pytest.raises(Exception, match="already running"):
            await backend.call("chat/send", {"project": "project-one", "text": "call-one-synthetic"})
        monkeypatch.setenv(
            "BCA_CLAUDE_BIN",
            str(fake_launcher(tmp_path, "tests.fakes.fake_claude", {"FAKE_CLAUDE_SCENARIO": "normal"})),
        )
        assert await backend.call("chat/send", {"project": "project-two", "text": "call-two-synthetic"})
        await backend.call("chat/stop", {"project": "project-one"})
        assert await first == {"status": "stopped", "project": "project-one"}
        assert pid_file.exists()
        hanging_pid = int(pid_file.read_text(encoding="utf-8"))
        for _ in range(100):
            if not pid_alive(hanging_pid):
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("chat/stop left the hanging fake Claude process alive")

    run(scenario())


def test_chat_second_turn_resumes_same_session_with_brief(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    argv_log = tmp_path / "claude-argv.json"
    stdin_log = tmp_path / "claude-stdin.jsonl"
    allow_fakes(
        monkeypatch,
        tmp_path,
        {
            "FAKE_CLAUDE_ARGV_LOG": str(argv_log),
            "FAKE_CLAUDE_STDIN_LOG": str(stdin_log),
        },
    )
    backend = make_backend(tmp_path, bq=FakeBqRunner())
    # The project folder is created lazily on first use; write the brief before the first turn.
    (tmp_path / "projects" / "project-one").mkdir(parents=True, exist_ok=True)
    (tmp_path / "projects" / "project-one" / "brief.md").write_text(
        "Synthetic brief", encoding="utf-8", newline="\n"
    )

    assert run(backend.call("chat/send", {"project": "project-one", "text": "first"})) == {
        "status": "completed",
        "project": "project-one",
    }
    first_argv = json.loads(argv_log.read_text(encoding="utf-8"))
    first_stdin = stdin_log.read_text(encoding="utf-8")
    first_session = first_argv[first_argv.index("--session-id") + 1]
    assert "Synthetic brief" in first_stdin

    assert run(backend.call("chat/send", {"project": "project-one", "text": "second"})) == {
        "status": "completed",
        "project": "project-one",
    }
    second_argv = json.loads(argv_log.read_text(encoding="utf-8"))
    second_stdin = stdin_log.read_text(encoding="utf-8")
    assert second_argv[second_argv.index("--resume") + 1] == first_session
    playbook = Path(second_argv[second_argv.index("--append-system-prompt-file") + 1])
    assert playbook.read_text(encoding="utf-8")
    assert "Synthetic brief" in second_stdin


def test_failed_first_turn_does_not_persist_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    allow_fakes(monkeypatch, tmp_path)
    backend = make_backend(tmp_path, bq=FakeBqRunner())
    monkeypatch.setenv("BCA_CLAUDE_BIN", str(tmp_path / "missing-claude"))
    with pytest.raises(RpcError):
        run(backend.call("chat/send", {"project": "project-one", "text": "first"}))
    session_file = tmp_path / "claude" / "sessions.json"
    assert not session_file.exists()
    argv_log = tmp_path / "claude-argv.json"
    monkeypatch.setenv(
        "BCA_CLAUDE_BIN",
        str(fake_launcher(tmp_path, "tests.fakes.fake_claude", {"FAKE_CLAUDE_ARGV_LOG": str(argv_log)})),
    )
    assert run(backend.call("chat/send", {"project": "project-one", "text": "second"}))
    assert "--session-id" in json.loads(argv_log.read_text(encoding="utf-8"))


def test_missing_conversation_on_resume_is_reseeded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    argv_log = tmp_path / "claude-argv.json"
    allow_fakes(monkeypatch, tmp_path, {"FAKE_CLAUDE_ARGV_LOG": str(argv_log)})
    backend = make_backend(tmp_path, bq=FakeBqRunner())
    assert run(backend.call("chat/send", {"project": "project-one", "text": "first"}))
    first = json.loads(argv_log.read_text(encoding="utf-8"))
    first_id = first[first.index("--session-id") + 1]
    monkeypatch.setenv(
        "BCA_CLAUDE_BIN",
        str(
            fake_launcher(
                tmp_path,
                "tests.fakes.fake_claude",
                {"FAKE_CLAUDE_SCENARIO": "missing_conversation", "FAKE_CLAUDE_ARGV_LOG": str(argv_log)},
            )
        ),
    )
    result = run(backend.call("chat/send", {"project": "project-one", "text": "brief retry"}))
    assert result == {"status": "completed", "project": "project-one"}
    second = json.loads(argv_log.read_text(encoding="utf-8"))
    assert "--session-id" in second and "--resume" not in second
    assert second[second.index("--session-id") + 1] != first_id


def test_brief_is_masked_before_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stdin_log = tmp_path / "claude-stdin.jsonl"
    allow_fakes(monkeypatch, tmp_path, {"FAKE_CLAUDE_STDIN_LOG": str(stdin_log)})
    (tmp_path / "projects" / "project-one").mkdir(parents=True)
    (tmp_path / "projects" / "project-one" / "brief.md").write_text(
        "Call 9876543210", encoding="utf-8", newline="\n"
    )
    backend = make_backend(tmp_path, bq=FakeBqRunner())
    run(backend.call("chat/send", {"project": "project-one", "text": "x"}))
    assert "[PHONE]" in stdin_log.read_text(encoding="utf-8")


def test_stdio_shutdown_during_turn_stops_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pid_file = tmp_path / "claude.pid"
    allow_fakes(
        monkeypatch, tmp_path, {"FAKE_CLAUDE_SCENARIO": "hang", "FAKE_CLAUDE_PID_FILE": str(pid_file)}
    )
    backend = make_backend(tmp_path, bq=FakeBqRunner())

    async def scenario() -> None:
        turn = asyncio.create_task(backend.call("chat/send", {"project": "project-one", "text": "x"}))
        for _ in range(200):
            if pid_file.exists():
                break
            await asyncio.sleep(0.01)
        assert pid_file.exists()
        await backend.call("shutdown", {})
        assert await turn == {"status": "stopped", "project": "project-one"}
        pid = int(pid_file.read_text(encoding="utf-8"))
        assert wait_until_gone(pid)
        assert not (tmp_path / "projects" / "project-one" / "chat" / "mcp.json").exists()

    run(scenario())


def test_claude_turn_failure_maps_to_claude_turn_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Use the guarded fakes (fake NER) so the turn reaches the Claude launch, then point
    # the Claude binary at a file that does not exist.
    allow_fakes(monkeypatch, tmp_path)
    backend = make_backend(tmp_path, bq=FakeBqRunner())
    monkeypatch.setenv("BCA_CLAUDE_BIN", str(tmp_path / "missing-claude"))
    with pytest.raises(RpcError) as error:
        run(backend.call("chat/send", {"project": "project-one", "text": "x"}))
    assert cast(dict[str, object], error.value.data)["reason"] == "claude_turn_failed"


def test_login_methods_spawn_private_binaries_with_app_profile_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = make_backend(tmp_path)
    claude = fake_launcher(tmp_path, "tests.fakes.fake_claude")
    codex = fake_launcher(tmp_path, "tests.fakes.fake_codex")
    backend._claude_binary = lambda: claude  # type: ignore[method-assign]
    backend._codex_binary = lambda: codex  # type: ignore[method-assign]
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.setenv(key, "synthetic-secret")
    calls: list[tuple[list[str], dict[str, object]]] = []

    class Child:
        pass

    def popen(command: list[str], **kwargs: object) -> Child:
        calls.append((command, kwargs))
        return Child()

    monkeypatch.setattr(subprocess, "Popen", popen)
    assert run(backend.call("auth/claudeLogin", {})) == {"started": True, "vendor": "claude"}
    assert run(backend.call("auth/codexLogin", {})) == {"started": True, "vendor": "codex"}
    assert len(calls) == 2
    claude_env = cast(dict[str, str], calls[0][1]["env"])
    codex_env = cast(dict[str, str], calls[1][1]["env"])
    assert str(tmp_path / "claude") in claude_env.get("CLAUDE_CONFIG_DIR", "")
    assert str(tmp_path / "codex") in codex_env.get("CODEX_HOME", "")
    for _command, kwargs in calls:
        env = cast(dict[str, str], kwargs["env"])
        assert not any(
            key in env
            for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_AUTH_TOKEN")
        )


def test_auth_status_reports_without_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backend = make_backend(tmp_path)
    monkeypatch.setattr(
        production, "claude_auth_status", lambda *_: AuthStatus(True, "claude.ai", "private-token")
    )

    class Client:
        async def request(self, method: str) -> dict[str, object]:
            return (
                {"account": {"type": "chatgpt"}}
                if method == "account/read"
                else {"rateLimits": {"primary": {"usedPercent": 42}, "planType": "pro"}}
            )

    class AppServer:
        client = Client()

        async def stop(self) -> None:
            return None

    backend._launch = lambda _path: _completed(AppServer())  # type: ignore[assignment]
    result = run(backend.call("auth/status", {}))
    serialized = json.dumps(result)
    assert result["codex"]["usagePercent"] == 42
    assert "private-token" not in serialized
    assert "credential" not in serialized.lower()
    assert "token" not in serialized.lower()


async def _completed(value: Any) -> Any:
    return value


def test_end_to_end_chat_over_stdio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    allow_fakes(monkeypatch, tmp_path)
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
    env = os.environ.copy()
    env.update({"BCA_DATA_DIR": str(tmp_path), "BCA_TEST_FAKES": "1", "FAKE_CLAUDE_SCENARIO": "normal"})
    process = subprocess.Popen(
        [sys.executable, "-m", "bca.api.stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    child_stdout = process.stdout
    child_stderr = process.stderr

    def read_message() -> dict[str, object]:
        line = child_stdout.readline()
        assert line, f"stdio child exited with {process.poll()}; stderr: {child_stderr.read()}"
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AssertionError(f"non-JSON stdout line: {line!r}") from exc
        assert isinstance(message, dict)
        return message

    process.stdin.write(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "projects/create",
                "params": {"project": "project-one", "title": "Synthetic"},
            }
        )
        + "\n"
    )
    process.stdin.flush()
    assert read_message()["id"] == 1
    process.stdin.write(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "chat/send",
                "params": {"project": "project-one", "text": "x"},
            }
        )
        + "\n"
    )
    process.stdin.flush()
    messages: list[dict[str, object]] = []
    while True:
        message = read_message()
        messages.append(message)
        if message.get("id") == 2:
            break
    assert any(message.get("method") == "chat/event" for message in messages)
    assert messages[-1]["result"] == {"status": "completed", "project": "project-one"}
    process.terminate()
    stdout, stderr = process.communicate(timeout=5)
    assert not stdout
    assert not stderr


def wait_until_gone(pid: int, seconds: float = 5.0) -> bool:
    """True once *pid* has exited. os.kill(pid, 0) is not a liveness check on Windows: signal 0
    is CTRL_C_EVENT there, sent to a console process group, and it interrupted the test run."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.05)
    return not pid_alive(pid)
