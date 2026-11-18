from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest


def script() -> Any:
    path = Path(__file__).parents[2] / "scripts" / "check_public.py"
    spec = importlib.util.spec_from_file_location("check_public", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("ghp_" + "a" * 24, "GitHub token"),
        ("github_pat_" + "a" * 22, "GitHub fine-grained token"),
        ("AKIA" + "A" * 16, "AWS access key"),
        ("ASIA" + "A" * 16, "AWS access key"),
        ("AIza" + "A" * 35, "Google API key"),
        ("PGP " + "PRIVATE KEY BLOCK", "PGP private-key block"),
    ],
)
def test_content_patterns(tmp_path: Path, text: str, reason: str) -> None:
    path = tmp_path / "item.txt"
    path.write_text(text)
    assert any(reason in item for item in script().scan_paths([path], tmp_path))


@pytest.mark.parametrize("name", [".ENV", "Auth.json", "Site.toml"])
def test_forbidden_names_case_insensitive(tmp_path: Path, name: str) -> None:
    path = tmp_path / name
    path.write_text("safe")
    assert script().scan_paths([path], tmp_path)


def test_utf16_latin1_nested_internal_and_logs(tmp_path: Path) -> None:
    utf16 = tmp_path / "utf16.txt"
    utf16.write_text("ghp_" + "a" * 24, encoding="utf-16")
    latin1 = tmp_path / "latin1.txt"
    latin1.write_bytes(b"caf\xe9 sk-" + b"a" * 24)
    nested = tmp_path / "docs" / "internal" / "notes.txt"
    nested.parent.mkdir(parents=True)
    nested.write_text("safe")
    log = tmp_path / "engine.log.1"
    log.write_text("safe")
    result = script().scan_paths([utf16, latin1, nested, log], tmp_path)
    assert any("GitHub token" in item for item in result)
    assert any("API key" in item for item in result)
    assert not any("forbidden tracked path" in item for item in result)
    assert any("tracked log file" in item for item in result)


def test_root_forbidden_path_and_binary(tmp_path: Path) -> None:
    internal = tmp_path / "internal"
    internal.mkdir()
    item = internal / "notes.txt"
    item.write_text("safe")
    binary = tmp_path / "binary.bin"
    binary.write_bytes(b"\x00" + b"sk-" + b"a" * 24)
    result = script().scan_paths([item, binary], tmp_path)
    assert any("forbidden tracked path" in item for item in result)
    assert len(result) == 1
