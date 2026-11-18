from __future__ import annotations

import os
from collections.abc import Mapping

ALLOWLIST = frozenset(
    [
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "TMPDIR",
        "HOME",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "USER",
        "USERNAME",
        "LOGNAME",
        "SHELL",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
        "OS",
    ]
)
DENIED_EXACT = frozenset(
    [
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_PROFILE",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_SIMPLE",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "CODEX_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_BEARER_TOKEN_BEDROCK",
    ]
)


class ChildEnvError(ValueError):
    """Raised when a caller attempts to pass a denied variable."""


def is_denied(name: str) -> bool:
    upper = name.upper()
    return (
        upper in DENIED_EXACT
        or upper
        in {
            "API_KEY",
            "AUTH_TOKEN",
            "ACCESS_TOKEN",
            "ANTHROPIC_CUSTOM_HEADERS",
        }
        or upper.endswith(("_API_KEY", "_AUTH_TOKEN", "_ACCESS_TOKEN", "_TOKEN", "_SECRET", "_KEY"))
    )


def build_child_env(
    extra: Mapping[str, str] | None = None, base: Mapping[str, str] | None = None
) -> dict[str, str]:
    source = os.environ if base is None else base
    insensitive = os.name == "nt"
    allowed = {name.upper() for name in ALLOWLIST} if insensitive else ALLOWLIST
    result = {
        name: value
        for name, value in source.items()
        if (name.upper() if insensitive else name) in allowed and not is_denied(name)
    }
    if extra:
        for name, value in extra.items():
            if is_denied(name):
                raise ChildEnvError(f"denied child environment variable: {name}")
            if not isinstance(value, str):
                raise ChildEnvError(f"child environment value for {name} must be a string")
            result[name] = value
    return result
