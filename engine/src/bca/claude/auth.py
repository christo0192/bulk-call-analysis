from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .launch import build_turn_env
from .profile import ClaudeProfile


@dataclass(frozen=True)
class AuthStatus:
    logged_in: bool
    auth_method: str | None
    config_directory: str | None

    @property
    def ok(self) -> bool:
        return self.logged_in and self.auth_method == "claude.ai"


def auth_status(binary: Path, profile: ClaudeProfile) -> AuthStatus:
    result = subprocess.run(
        [str(binary), "auth", "status", "--json"],
        env=build_turn_env(profile),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return AuthStatus(False, None, None)
    try:
        data: Any = json.loads(result.stdout)
    except json.JSONDecodeError:
        return AuthStatus(False, None, None)
    return AuthStatus(bool(data.get("loggedIn", False)), data.get("authMethod"), data.get("configDirectory"))


def start_login(binary: Path, profile: ClaudeProfile) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [str(binary), "auth", "login", "--claudeai"], env=build_turn_env(profile), text=True
    )
