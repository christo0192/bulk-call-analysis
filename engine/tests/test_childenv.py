from __future__ import annotations

import pytest

from bca import childenv


def test_allowlist_and_extra() -> None:
    result = childenv.build_child_env({"CUSTOM": "yes"}, {"PATH": "p", "RANDOM": "no"})
    assert result == {"PATH": "p", "CUSTOM": "yes"}


def test_denies_exact_and_patterns() -> None:
    base = {name: "x" for name in childenv.DENIED_EXACT} | {
        "THING_API_KEY": "x",
        "THING_AUTH_TOKEN": "x",
        "THING_ACCESS_TOKEN": "x",
        "API_KEY": "x",
        "AUTH_TOKEN": "x",
        "ACCESS_TOKEN": "x",
        "ANTHROPIC_CUSTOM_HEADERS": "x",
        "THING_TOKEN": "x",
        "THING_SECRET": "x",
        "THING_KEY": "x",
    }
    assert childenv.build_child_env(base=base) == {}
    with pytest.raises(childenv.ChildEnvError):
        childenv.build_child_env({"openai_api_key": "x"})


@pytest.mark.parametrize("name", ["GITHUB_TOKEN", "GH_TOKEN", "HF_TOKEN", "OPENAI_KEY"])
def test_denies_token_suffixes(name: str) -> None:
    assert childenv.is_denied(name)


def test_windows_case_handling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(childenv.os, "name", "nt")  # type: ignore[attr-defined]
    assert childenv.build_child_env(base={"path": "p", "random": "x"}) == {"path": "p"}


def test_non_string_extra_rejected() -> None:
    with pytest.raises(childenv.ChildEnvError):
        childenv.build_child_env({"CUSTOM": 1})  # type: ignore[dict-item]
