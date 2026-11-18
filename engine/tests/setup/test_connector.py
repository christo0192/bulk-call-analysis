import json
from pathlib import Path

from bca.config import BigQueryConfig
from bca.setup.connector import detect


def test_defaults_use_configured_names_and_existence_only(tmp_path: Path) -> None:
    home = tmp_path / "home"
    connector = home / "mcp-servers" / "sample" / ".venv" / "bin" / "sample"
    key = home / ".gcp-keys" / "sample.json"
    connector.parent.mkdir(parents=True)
    connector.touch()
    key.parent.mkdir()
    key.write_text("not opened by detector", encoding="utf-8")
    result = detect(
        BigQueryConfig("project", connector_dir=None, key_path=None),
        connector_name="sample",
        key_name="sample.json",
        system="Darwin",
        home=home,
        env={},
    )
    assert result.status == "located"
    assert result.connector == connector
    assert result.key == key


def test_desktop_matching_entry(tmp_path: Path) -> None:
    home = tmp_path / "home"
    command = tmp_path / "connector"
    key = tmp_path / "key.json"
    command.touch()
    key.touch()
    config = home / "Library/Application Support/Claude/claude_desktop_config.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "sample": {
                        "command": str(command),
                        "env": {"GOOGLE_APPLICATION_CREDENTIALS": str(key), "SECRET": "masked"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    result = detect(BigQueryConfig("project"), connector_name="sample", system="Darwin", home=home, env={})
    assert result.status == "located"
    assert result.key == key
