from __future__ import annotations

import json
import os
import platform
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bca.config import BigQueryConfig


@dataclass(frozen=True)
class ConnectorResult:
    connector: Path | None
    key: Path | None
    status: str
    config_path: Path | None = None


def _candidate_config(*, env: Mapping[str, str], home: Path, system: str) -> list[Path]:
    if system == "Windows":
        paths = [
            Path(env.get("APPDATA", str(home / "AppData" / "Roaming")))
            / "Claude"
            / "claude_desktop_config.json"
        ]
        packages = Path(env.get("LOCALAPPDATA", str(home / "AppData" / "Local"))) / "Packages"
        paths.extend(packages.glob("Claude_*/LocalCache/Roaming/Claude/claude_desktop_config.json"))
        return paths
    if system == "Darwin":
        return [home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"]
    return []


def _entry(path: Path, name: str) -> tuple[str | None, Path | None]:
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
        entry = raw.get("mcpServers", {}).get(name, {}) if isinstance(raw, dict) else {}
        command = entry.get("command") if isinstance(entry, dict) else None
        args = entry.get("args", []) if isinstance(entry, dict) else []
        command_path = Path(command) if isinstance(command, str) else None
        if command_path is not None and not command_path.exists():
            command_path = next(
                (Path(arg) for arg in args if isinstance(arg, str) and Path(arg).exists()), None
            )
        env_values = entry.get("env", {}) if isinstance(entry, dict) else {}
        key = env_values.get("GOOGLE_APPLICATION_CREDENTIALS") if isinstance(env_values, dict) else None
        return (
            str(command_path) if command_path is not None else None,
            Path(key).expanduser() if isinstance(key, str) else None,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, None


def detect(
    settings: BigQueryConfig,
    *,
    connector_name: str | None = None,
    key_name: str | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    system: str | None = None,
) -> ConnectorResult:
    values = os.environ if env is None else env
    home_path = Path.home() if home is None else home
    os_name = platform.system() if system is None else system
    connector_name = (
        connector_name
        or settings.connector_name
        or (settings.connector_dir.name if settings.connector_dir else None)
    )
    key_name = key_name or settings.key_name or (settings.key_path.name if settings.key_path else None)
    connector = settings.connector_dir
    key = settings.key_path
    if connector is None and connector_name:
        if os_name == "Windows":
            connector = (
                Path(values.get("USERPROFILE", str(home_path)))
                / "mcp-servers"
                / connector_name
                / ".venv"
                / "Scripts"
                / f"{connector_name}.exe"
            )
        else:
            connector = home_path / "mcp-servers" / connector_name / ".venv" / "bin" / connector_name
    if key is None and key_name:
        key = Path(values.get("USERPROFILE", str(home_path))) / ".gcp-keys" / key_name
    if connector is not None and key is not None and connector.exists() and key.exists():
        return ConnectorResult(connector, key, "located")
    for config_path in _candidate_config(env=values, home=home_path, system=os_name):
        if config_path.is_file():
            command, config_key = _entry(config_path, connector_name or "")
            found_connector = Path(command) if command else None
            if found_connector and config_key and found_connector.exists() and config_key.exists():
                return ConnectorResult(found_connector, config_key, "located", config_path)
    return ConnectorResult(
        connector if connector and connector.exists() else None,
        key if key and key.exists() else None,
        "needs file picker",
    )


def recheck(settings: BigQueryConfig, **kwargs: Any) -> ConnectorResult:
    return detect(settings, **kwargs)
