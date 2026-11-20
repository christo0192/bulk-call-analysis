from __future__ import annotations

import json
import os
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
        text=True,
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        return AuthStatus(False, None, None)
    try:
        data: Any = json.loads(result.stdout)
    except json.JSONDecodeError:
        return AuthStatus(False, None, None)
    return AuthStatus(bool(data.get("loggedIn", False)), data.get("authMethod"), data.get("configDirectory"))


def start_login(binary: Path, profile: ClaudeProfile) -> subprocess.Popen[str]:
    kwargs: dict[str, Any] = {
        "env": build_turn_env(profile),
        "text": True,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = int(getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
    return subprocess.Popen([str(binary), "auth", "login", "--claudeai"], **kwargs)
