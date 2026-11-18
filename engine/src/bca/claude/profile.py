from __future__ import annotations

import json
from pathlib import Path

# Confirmed in the official Claude Code settings reference (2026-11-18).
SETTING_CONNECTORS = "disableClaudeAiConnectors"
SETTING_SKILLS = "syncClaudeAiSkills"
SETTING_PLUGINS = "syncClaudeAiPlugins"
SETTING_HOOKS = "hooks"
SETTING_SWITCHING = "modelPicker"
SETTING_FAST = "fastMode"
SETTING_CLEANUP = "cleanupPeriodDays"

# Claude Code documents this environment switch for auto-memory. Ultracode is
# deliberately also blocked by an empty tool set in launch.py; no public
# setting key is assumed for an experimental feature.
ENV_DISABLE_MEMORY = "CLAUDE_CODE_DISABLE_AUTO_MEMORY"


class ClaudeProfile:
    def __init__(self, config_dir: Path, work_root: Path, cleanup_period_days: int = 30) -> None:
        self.config_dir = config_dir
        self.work_root = work_root
        self.cleanup_period_days = cleanup_period_days

    @property
    def settings_path(self) -> Path:
        return self.config_dir / "settings.json"

    def write_settings(self) -> Path:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        settings: dict[str, object] = {
            SETTING_CONNECTORS: True,
            SETTING_SKILLS: False,
            SETTING_PLUGINS: False,
            SETTING_HOOKS: {},
            SETTING_SWITCHING: False,
            SETTING_FAST: False,
            SETTING_CLEANUP: self.cleanup_period_days,
        }
        self.settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
        return self.settings_path

    def project_workdir(self, project_id: str) -> Path:
        if not project_id or Path(project_id).name != project_id:
            raise ValueError("project_id must be a single non-empty path component")
        path = self.work_root / project_id
        path.mkdir(parents=True, exist_ok=False)
        return path
