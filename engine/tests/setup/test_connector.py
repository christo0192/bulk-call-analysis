import json
from pathlib import Path
from typing import Any, cast

import pytest

from bca.config import BigQueryConfig
from bca.setup.connector import detect, recheck


@pytest.mark.parametrize("system", ["Windows", "Darwin", "Linux"])
def test_defaults_are_found_per_os(tmp_path: Path, system: str) -> None:
    home = tmp_path / "home"
    name = "sample"
    connector = (
        home
        / "mcp-servers"
        / name
        / ".venv"
        / ("Scripts" if system == "Windows" else "bin")
        / (name + (".exe" if system == "Windows" else ""))
    )
    key = home / ".gcp-keys" / "sample.json"
    connector.parent.mkdir(parents=True)
    connector.touch()
    key.parent.mkdir()
    key.touch()
    result = detect(
        BigQueryConfig("project"), connector_name=name, key_name=key.name, system=system, home=home, env={}
    )
    assert result.status == "located"
    assert result.connector == connector
    assert result.key == key


def _write_desktop_config(path: Path, entry: dict[str, object]) -> None:
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "wanted": entry,
                    "other": {"env": {"GOOGLE_APPLICATION_CREDENTIALS": "secret-looking"}},
                }
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize("msix", [False, True])
def test_desktop_classic_and_msix_paths_only_parse_matching_entry(tmp_path: Path, msix: bool) -> None:
    home = tmp_path / "home"
    base = home / "AppData/Local" if msix else home / "AppData/Roaming/Claude"
    config = base / (
        "Packages/Claude_test/LocalCache/Roaming/Claude/claude_desktop_config.json"
        if msix
        else "claude_desktop_config.json"
    )
    command, key = tmp_path / "wanted.exe", tmp_path / "key.json"
    command.touch()
    key.touch()
    _write_desktop_config(
        config,
        {
            "command": str(command),
            "env": {"GOOGLE_APPLICATION_CREDENTIALS": str(key), "OTHER": "not returned"},
        },
    )
    result = detect(BigQueryConfig("project"), connector_name="wanted", system="Windows", home=home, env={})
    assert result.status == "located"
    assert result.connector == command
    assert result.key == key
    assert result.config_path == config


def test_desktop_config_rejects_bad_shape_and_derives_uv_directory_entry(tmp_path: Path) -> None:
    home = tmp_path / "home"
    config = home / "AppData/Roaming/Claude/claude_desktop_config.json"
    server = tmp_path / "server"
    connector = server / ".venv/Scripts/wanted.exe"
    connector.parent.mkdir(parents=True)
    connector.touch()
    key = tmp_path / "key.json"
    key.touch()
    config.parent.mkdir(parents=True)
    config.write_text(
        "\ufeff"
        + json.dumps(
            {
                "mcpServers": {
                    "wanted": {
                        "command": str(tmp_path / "uv.exe"),
                        "args": ["--directory", str(server), "run", "wanted"],
                        "env": {"GOOGLE_APPLICATION_CREDENTIALS": str(key)},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    result = detect(BigQueryConfig("project"), connector_name="wanted", system="Windows", home=home, env={})
    assert result.connector == connector
    config.write_text(json.dumps({"mcpServers": []}), encoding="utf-8")
    assert (
        detect(BigQueryConfig("project"), connector_name="wanted", system="Windows", home=home, env={}).status
        == "needs file picker"
    )


def test_key_is_checked_for_existence_but_never_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = tmp_path / "key.json"
    connector = tmp_path / "connector"
    key.touch()
    connector.touch()
    original = Path.open

    def guarded(self: Path, *args: object, **kwargs: object) -> Any:
        if self == key:
            raise AssertionError("credential file was opened")
        return cast(Any, original)(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", cast(Any, guarded))
    result = detect(
        BigQueryConfig("project", connector_dir=connector, key_path=key),
        system="Linux",
        env={},
        home=tmp_path,
    )
    assert result.status == "located"


def test_needs_file_picker_and_recheck(tmp_path: Path) -> None:
    connector = tmp_path / "missing"
    key = tmp_path / "missing.json"
    settings = BigQueryConfig("project", connector_dir=connector, key_path=key)
    result = detect(settings, system="Linux", env={}, home=tmp_path)
    assert result.status == "needs file picker"
    connector.mkdir()
    key.touch()
    assert recheck(settings, system="Linux", env={}, home=tmp_path).status == "located"
