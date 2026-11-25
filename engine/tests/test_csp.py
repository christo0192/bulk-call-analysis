from __future__ import annotations

import json
from pathlib import Path


def test_tauri_csp_is_exact() -> None:
    config = json.loads(
        (Path(__file__).parents[2] / "shell" / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8")
    )
    assert config["app"]["security"]["csp"] == (
        "default-src 'self'; connect-src 'self' ipc: http://ipc.localhost; "
        "img-src 'self' asset: data:; style-src 'self'; script-src 'self'"
    )
