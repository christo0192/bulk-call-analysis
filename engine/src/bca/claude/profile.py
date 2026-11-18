from __future__ import annotations

import json
from pathlib import Path

# Confirmed in the official Claude Code settings reference (2026-11-18).
SETTING_CONNECTORS = "disableClaudeAiConnectors"
SETTING_SKILLS = "syncClaudeAiSkills"
SETTING_PLUGINS = "syncClaudeAiPlugins"
SETTING_HOOKS = "hooks"
SETTING_SWITCHING = "switchModelsOnFlag"
SETTING_AVAILABLE_MODELS = "availableModels"
SETTING_AVAILABLE_MODELS_MATCH = "availableModelsMatch"
SETTING_FAST = "fastMode"
SETTING_CLEANUP = "cleanupPeriodDays"
SETTING_ULTRACODE = "ultracode"
SETTING_WORKFLOWS = "disableWorkflows"

# The setting names and value types below are from the Claude Code settings
# reference. The stream shapes checked by stream.py come from the Agent SDK
# TypeScript message union, not from this application.
ENV_DISABLE_MEMORY = "CLAUDE_CODE_DISABLE_AUTO_MEMORY"


class ClaudeProfile:
    def __init__(
        self,
        config_dir: Path,
        work_root: Path,
        cleanup_period_days: int = 30,
        configured_model: str = "claude-opus-5-5",
    ) -> None:
        if not isinstance(cleanup_period_days, int) or isinstance(cleanup_period_days, bool):
            raise TypeError("cleanup_period_days must be an integer")
        if not isinstance(configured_model, str) or not configured_model.startswith("claude-"):
            raise ValueError("configured_model must be a Claude model id")
        self.config_dir = config_dir
        self.work_root = work_root
        self.cleanup_period_days = cleanup_period_days
        self.configured_model = configured_model

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
            SETTING_AVAILABLE_MODELS: [self.configured_model],
            SETTING_AVAILABLE_MODELS_MATCH: "exact",
            SETTING_FAST: False,
            SETTING_CLEANUP: self.cleanup_period_days,
            SETTING_ULTRACODE: False,
            SETTING_WORKFLOWS: True,
        }
        self.settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
        return self.settings_path

    def project_workdir(self, project_id: str) -> Path:
        if not project_id or Path(project_id).name != project_id:
            raise ValueError("project_id must be a single non-empty path component")
        path = self.work_root / project_id
        path.mkdir(parents=True, exist_ok=False)
        return path
