import json
import sys

from bca.luna.appserver import DISABLED_FEATURES, LOCKDOWN_OVERRIDES, CodexLaunch, build_command, start


def test_build_command_contains_lockdown_and_disabled_features(tmp_path) -> None:
    command = build_command(CodexLaunch(sys.executable, tmp_path, tmp_path))
    for key, value in LOCKDOWN_OVERRIDES:
        assert f"{key}={value}" in command
    for feature in DISABLED_FEATURES:
        assert feature in command


def test_launch_strips_api_keys_and_cleanly_stops(tmp_path, monkeypatch) -> None:
    import asyncio

    asyncio.run(_test_launch_strips_api_keys_and_cleanly_stops(tmp_path, monkeypatch))


async def _test_launch_strips_api_keys_and_cleanly_stops(tmp_path, monkeypatch) -> None:
    for name in ("OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(name, "secret")
    env_report = tmp_path / "env.json"
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps({"models": ["codex"], "env_report": str(env_report)}), encoding="utf-8")
    server = await start(
        CodexLaunch(
            (sys.executable, "-m", "tests.fakes.fake_codex", "--scenario", str(scenario)),
            tmp_path / "home",
            tmp_path,
        )
    )
    assert await server.client.request("account/rateLimits/read")
    assert await server.client.request("model/list") == {"models": ["codex"]}
    await server.stop()
    assert server.returncode is not None
    child_env = json.loads(env_report.read_text(encoding="utf-8"))
    assert "CODEX_HOME" in child_env
    assert all(name not in child_env for name in ("OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_API_KEY"))
