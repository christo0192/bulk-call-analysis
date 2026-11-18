from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from bca.config import ConfigError, find_config, load_config


def example(tmp_path: Path) -> Path:
    source = Path(__file__).parents[2] / "config" / "site.example.toml"
    target = tmp_path / "site.toml"
    shutil.copy(source, target)
    return target


def test_valid_example_loads(tmp_path: Path) -> None:
    assert load_config(example(tmp_path)).bigquery.project == "your-gcp-project"


def test_config_validation(tmp_path: Path) -> None:
    path = example(tmp_path)
    original = path.read_text()
    cases = [
        (original + "\n[extra]\nvalue = 1\n", "extra"),
        (original.replace("95", "40"), "codex_stop_percent"),
        (
            original.replace("warn_bytes = 1073741824", "warn_bytes = 10").replace(
                "reject_bytes = 10737418240", "reject_bytes = 5"
            ),
            "warn_bytes",
        ),
        (original.replace('claude_effort = "high"', 'claude_effort = "invalid"'), "claude_effort"),
    ]
    for text, key in cases:
        path.write_text(text)
        with pytest.raises(ConfigError, match=key):
            load_config(path)
        path.write_text(original)


def test_tilde_expands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = example(tmp_path)
    path.write_text(
        path.read_text().replace('# connector_dir = "~/connectors"', 'connector_dir = "~/connectors"')
    )
    assert load_config(path).bigquery.connector_dir == Path("~/connectors").expanduser()


@pytest.mark.parametrize("payload", [b"\xef\xbb\xbf" + b"", b"\xe9"])
def test_invalid_or_bom_toml(tmp_path: Path, payload: bytes) -> None:
    path = example(tmp_path)
    if payload == b"\xef\xbb\xbf":
        path.write_bytes(payload + path.read_bytes())
        assert load_config(path).bigquery.project == "your-gcp-project"
    else:
        path.write_bytes(payload)
        with pytest.raises(ConfigError, match="invalid TOML"):
            load_config(path)


def test_toml_typo_and_unknown_key(tmp_path: Path) -> None:
    path = example(tmp_path)
    path.write_text(path.read_text().replace("[bigquery]", '[bigquery]\nlocatoin = "EU"'))
    with pytest.raises(ConfigError, match=r"bigquery\.locatoin"):
        load_config(path)
    path.write_text("not = [valid")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(path)


def test_bca_config_missing_does_not_fallback(tmp_path: Path) -> None:
    missing = tmp_path / "missing.toml"
    with pytest.raises(ConfigError, match=r"missing\.toml"):
        find_config(env={"BCA_CONFIG": str(missing)}, cwd=tmp_path, home=tmp_path)


@pytest.mark.parametrize(
    "system",
    [
        "Windows",
        "Darwin",
        "Linux",
    ],
)
def test_find_config_order(tmp_path: Path, system: str) -> None:
    env = {"BCA_CONFIG": str(tmp_path / "env.toml")}
    home = tmp_path / "home"
    cwd = tmp_path / "a" / "b"
    if system == "Windows":
        env["LOCALAPPDATA"] = str(tmp_path / "local")
        user_path = tmp_path / "local" / "BulkCallAnalysis" / "site.toml"
    elif system == "Darwin":
        user_path = home / "Library" / "Application Support" / "BulkCallAnalysis" / "site.toml"
    elif system == "Linux":
        env["XDG_CONFIG_HOME"] = str(tmp_path / "xdg")
        user_path = tmp_path / "xdg" / "bulkcallanalysis" / "site.toml"
    else:
        raise AssertionError(f"unsupported test system: {system}")

    env_path = Path(env["BCA_CONFIG"])
    env_path.write_text("x")
    assert find_config(system=system, env=env, home=home, cwd=cwd) == env_path

    env_path.unlink()
    env.pop("BCA_CONFIG")
    config_path = tmp_path / "config" / "site.toml"
    config_path.parent.mkdir()
    config_path.write_text("x")
    assert find_config(system=system, env=env, home=home, cwd=cwd) == config_path

    config_path.unlink()
    user_path.parent.mkdir(parents=True)
    user_path.write_text("x")
    assert find_config(system=system, env=env, home=home, cwd=cwd) == user_path

    user_path.unlink()
    with pytest.raises(ConfigError) as error:
        find_config(system=system, env=env, home=home, cwd=cwd)
    message = str(error.value)
    assert str(config_path) in message
    assert str(user_path) in message
