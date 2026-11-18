import json
import sys
from pathlib import Path

import pytest

from bca.luna.appserver import DISABLED_FEATURES, LOCKDOWN_OVERRIDES, CodexLaunch, build_command, start
from tests.fakes import fake_codex


def test_build_command_contains_lockdown_and_disabled_features(tmp_path: Path) -> None:
    command = build_command(CodexLaunch(Path(sys.executable), tmp_path, tmp_path))
    for key, value in LOCKDOWN_OVERRIDES:
        assert f"{key}={value}" in command
    for feature in DISABLED_FEATURES:
        assert feature in command


def test_launch_strips_api_keys_and_cleanly_stops(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    asyncio.run(_test_launch_strips_api_keys_and_cleanly_stops(tmp_path, monkeypatch))


def test_launch_passes_lockdown_arguments_to_fake(tmp_path: Path) -> None:
    import asyncio

    asyncio.run(_test_launch_passes_lockdown_arguments_to_fake(tmp_path))


async def _test_launch_passes_lockdown_arguments_to_fake(tmp_path: Path) -> None:
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps({"models": ["codex"]}), encoding="utf-8")
    server = await start(
        CodexLaunch(
            (sys.executable, str(Path(fake_codex.__file__)), "--scenario", str(scenario)),
            tmp_path / "home",
            tmp_path,
        )
    )
    await server.stop()
    log = json.loads(Path(str(scenario) + ".methods.json").read_text(encoding="utf-8"))
    startup = log[0]
    assert all(
        any(value.startswith(f"{key}=") for value in startup["argv_overrides"])
        for key, _ in LOCKDOWN_OVERRIDES
    )
    assert startup["argv_disabled"] == list(DISABLED_FEATURES)


async def _test_launch_strips_api_keys_and_cleanly_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(name, "secret")
    env_report = tmp_path / "env.json"
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps({"models": ["codex"], "env_report": str(env_report)}), encoding="utf-8")
    server = await start(
        CodexLaunch(
            (sys.executable, str(Path(fake_codex.__file__)), "--scenario", str(scenario)),
            tmp_path / "home",
            tmp_path,
        )
    )
    assert await server.client.request("account/rateLimits/read")
    model_list = await server.client.request("model/list")
    assert model_list["nextCursor"] is None
    assert model_list["data"][0]["id"] == "codex"
    await server.stop()
    assert server.returncode is not None
    child_env = json.loads(env_report.read_text(encoding="utf-8"))
    assert "CODEX_HOME" in child_env
    assert all(name not in child_env for name in ("OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_API_KEY"))
