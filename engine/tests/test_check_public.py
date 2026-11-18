from __future__ import annotations

import importlib.util
from pathlib import Path


def script():
    path = Path(__file__).parents[2] / "scripts" / "check_public.py"
    spec = importlib.util.spec_from_file_location("check_public", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_scan_paths(tmp_path: Path) -> None:
    good = tmp_path / "good.txt"
    good.write_text("safe")
    bad = tmp_path / "bad.txt"
    bad.write_text("ghp_" + "a" * 24)
    secret_name = tmp_path / "auth.json"
    secret_name.write_text("safe")
    assert script().scan_paths([good], tmp_path) == []
    assert script().scan_paths([bad], tmp_path)
    assert script().scan_paths([secret_name], tmp_path)


def test_forbidden_path_and_binary(tmp_path: Path) -> None:
    internal = tmp_path / "internal"
    internal.mkdir()
    item = internal / "notes.txt"
    item.write_text("safe")
    binary = tmp_path / "binary.bin"
    binary.write_bytes(b"\x00" + b"sk-" + b"a" * 24)
    result = script().scan_paths([item, binary], tmp_path)
    assert any("forbidden tracked path" in item for item in result)
    assert len(result) == 1
