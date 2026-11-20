from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from bca.claude.launch import TurnGuardError
from tests.api.test_production_chat import allow_fakes, make_backend


@pytest.mark.parametrize(
    "patch",
    [
        {"model": "claude-sonnet-4-5"},
        {"tools": ["Bash"]},
        {"apiKeySource": "key"},
        {"fast_mode_state": "on"},
        {"mcp_servers": [{"name": "outside", "status": "connected"}]},
    ],
    ids=["different-model", "extra-tool", "api-key", "fast-mode", "foreign-mcp"],
)
def test_unexpected_claude_init_aborts_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patch: dict[str, object]
) -> None:
    allow_fakes(monkeypatch, tmp_path, {"FAKE_CLAUDE_INIT_PATCH": json.dumps(patch)})
    backend = make_backend(tmp_path)
    with pytest.raises(Exception) as error:
        asyncio.run(backend.call("chat/send", {"project": "project-one", "text": "synthetic"}))
    assert isinstance(error.value, (TurnGuardError, RuntimeError)) or "claude" in str(error.value).lower()
