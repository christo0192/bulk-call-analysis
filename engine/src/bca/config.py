from __future__ import annotations

import os
import platform
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

Effort = Literal["low", "medium", "high", "xhigh", "max"]


class ConfigError(ValueError):
    """Raised when site configuration is missing or invalid."""


@dataclass(frozen=True)
class BigQueryConfig:
    project: str
    location: str = "US"
    max_bytes_billed: int = 50 * 1024**3
    warn_bytes: int = 1 * 1024**3
    reject_bytes: int = 10 * 1024**3
    connector_dir: Path | None = None
    key_path: Path | None = None


@dataclass(frozen=True)
class RecipeConfig:
    transcripts_table: str
    calls_table: str
    lead_id_column: str
    call_id_column: str
    transcript_text_column: str
    call_start_column: str
    call_duration_column: str
    duration_unit: Literal["ms", "s"] = "ms"
    max_chars_per_lead: int = 400_000
    drop_calls_shorter_than_s: int = 180
    advisor_column: str | None = None
    transcript_status_column: str | None = None


@dataclass(frozen=True)
class ModelsConfig:
    claude_model: str = "claude-opus-5-5"
    claude_effort: Effort = "high"
    luna_model: str = "gpt-5.6-luna"
    luna_effort: Effort = "high"


@dataclass(frozen=True)
class LimitsConfig:
    codex_stop_percent: int = 95
    claude_turn_max_turns: int = 25
    lead_timeout_s: int = 1800
    lead_max_attempts: int = 2


@dataclass(frozen=True)
class RetentionConfig:
    masked_cache_days: int = 30
    chat_days: int = 90


@dataclass(frozen=True)
class MaskingConfig:
    roster_file: Path | None = None


@dataclass(frozen=True)
class SiteConfig:
    bigquery: BigQueryConfig
    recipe: RecipeConfig
    models: ModelsConfig
    limits: LimitsConfig
    retention: RetentionConfig
    masking: MaskingConfig


def _error(key: str, message: str) -> ConfigError:
    return ConfigError(f"{key}: {message}")


def _table(data: dict[str, Any], section: str, allowed: set[str]) -> dict[str, Any]:
    unknown = set(data) - allowed
    if unknown:
        key = f"{section}.{sorted(unknown)[0]}"
        raise _error(key, "unknown key")
    return data


def _str(data: dict[str, Any], section: str, key: str, default: str | None = None) -> str:
    value = data.get(key, default)
    if not isinstance(value, str) or not value:
        raise _error(f"{section}.{key}", "must be a non-empty string")
    return value


def _int(data: dict[str, Any], section: str, key: str, default: int | None = None) -> int:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _error(f"{section}.{key}", "must be a positive integer")
    return value


def _path(data: dict[str, Any], section: str, key: str) -> Path | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _error(f"{section}.{key}", "must be a path string")
    return Path(value).expanduser()


def _optional_str(data: dict[str, Any], section: str, key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise _error(f"{section}.{key}", "must be a non-empty string")
    return value


def load_config(path: Path) -> SiteConfig:
    try:
        raw = tomllib.loads(path.read_bytes().decode("utf-8-sig"))
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read configuration: {exc}") from exc
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("configuration: must be a TOML table")
    sections = {"bigquery", "recipe", "models", "limits", "retention", "masking"}
    unknown = set(raw) - sections
    if unknown:
        raise _error(sorted(unknown)[0], "unknown section")
    missing = sections - set(raw)
    if missing:
        raise _error(f"{sorted(missing)[0]}", "missing section")
    tables = {name: raw[name] for name in sections}
    if any(not isinstance(value, dict) for value in tables.values()):
        bad = next(name for name, value in tables.items() if not isinstance(value, dict))
        raise _error(bad, "must be a table")
    bq = cast(dict[str, Any], tables["bigquery"])
    recipe = cast(dict[str, Any], tables["recipe"])
    models = cast(dict[str, Any], tables["models"])
    limits = cast(dict[str, Any], tables["limits"])
    retention = cast(dict[str, Any], tables["retention"])
    masking = cast(dict[str, Any], tables["masking"])
    _table(
        bq,
        "bigquery",
        {
            "project",
            "location",
            "max_bytes_billed",
            "warn_bytes",
            "reject_bytes",
            "connector_dir",
            "key_path",
        },
    )
    _table(
        recipe,
        "recipe",
        {
            "transcripts_table",
            "calls_table",
            "lead_id_column",
            "call_id_column",
            "transcript_text_column",
            "call_start_column",
            "call_duration_column",
            "advisor_column",
            "transcript_status_column",
            "duration_unit",
            "max_chars_per_lead",
            "drop_calls_shorter_than_s",
        },
    )
    _table(models, "models", {"claude_model", "claude_effort", "luna_model", "luna_effort"})
    _table(
        limits,
        "limits",
        {"codex_stop_percent", "claude_turn_max_turns", "lead_timeout_s", "lead_max_attempts"},
    )
    _table(retention, "retention", {"masked_cache_days", "chat_days"})
    _table(masking, "masking", {"roster_file"})
    max_bytes = _int(bq, "bigquery", "max_bytes_billed", 50 * 1024**3)
    warn = _int(bq, "bigquery", "warn_bytes", 1 * 1024**3)
    reject = _int(bq, "bigquery", "reject_bytes", 10 * 1024**3)
    if not warn < reject < max_bytes:
        raise _error(
            "bigquery.warn_bytes", "must be less than reject_bytes, which must be less than max_bytes_billed"
        )
    duration = recipe.get("duration_unit", "ms")
    if duration not in ("ms", "s"):
        raise _error("recipe.duration_unit", "must be 'ms' or 's'")
    effort_values: dict[str, Effort] = {}
    for key in ("claude_effort", "luna_effort"):
        effort = _str(models, "models", key, "high")
        if effort not in ("low", "medium", "high", "xhigh", "max"):
            raise _error(f"models.{key}", "must be one of low|medium|high|xhigh|max")
        effort_values[key] = cast(Effort, effort)
    stop = _int(limits, "limits", "codex_stop_percent", 95)
    if not 50 <= stop <= 99:
        raise _error("limits.codex_stop_percent", "must be between 50 and 99")
    return SiteConfig(
        BigQueryConfig(
            _str(bq, "bigquery", "project"),
            _str(bq, "bigquery", "location", "US"),
            max_bytes,
            warn,
            reject,
            _path(bq, "bigquery", "connector_dir"),
            _path(bq, "bigquery", "key_path"),
        ),
        RecipeConfig(
            _str(recipe, "recipe", "transcripts_table"),
            _str(recipe, "recipe", "calls_table"),
            _str(recipe, "recipe", "lead_id_column"),
            _str(recipe, "recipe", "call_id_column"),
            _str(recipe, "recipe", "transcript_text_column"),
            _str(recipe, "recipe", "call_start_column"),
            _str(recipe, "recipe", "call_duration_column"),
            cast(Literal["ms", "s"], duration),
            _int(recipe, "recipe", "max_chars_per_lead", 400_000),
            _int(recipe, "recipe", "drop_calls_shorter_than_s", 180),
            _optional_str(recipe, "recipe", "advisor_column"),
            _optional_str(recipe, "recipe", "transcript_status_column"),
        ),
        ModelsConfig(
            _str(models, "models", "claude_model", "claude-opus-5-5"),
            effort_values["claude_effort"],
            _str(models, "models", "luna_model", "gpt-5.6-luna"),
            effort_values["luna_effort"],
        ),
        LimitsConfig(
            stop,
            _int(limits, "limits", "claude_turn_max_turns", 25),
            _int(limits, "limits", "lead_timeout_s", 1800),
            _int(limits, "limits", "lead_max_attempts", 2),
        ),
        RetentionConfig(
            _int(retention, "retention", "masked_cache_days", 30),
            _int(retention, "retention", "chat_days", 90),
        ),
        MaskingConfig(_path(masking, "masking", "roster_file")),
    )


def find_config(
    *,
    system: str | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    cwd: Path | None = None,
) -> Path:
    system = platform.system() if system is None else system
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    cwd = Path.cwd() if cwd is None else cwd
    checked: list[Path] = []
    env_path = env.get("BCA_CONFIG")
    if env_path:
        candidate = Path(env_path).expanduser()
        checked.append(candidate)
        if candidate.is_file():
            return candidate
        raise ConfigError(f"BCA_CONFIG path is not a file: {candidate}")
    current = cwd.resolve()
    for folder in (current, *current.parents):
        candidate = folder / "config" / "site.toml"
        checked.append(candidate)
        if candidate.is_file():
            return candidate
    if system == "Windows":
        base = Path(env.get("LOCALAPPDATA", home / "AppData" / "Local"))
        candidate = base / "BulkCallAnalysis" / "site.toml"
    elif system == "Darwin":
        candidate = home / "Library" / "Application Support" / "BulkCallAnalysis" / "site.toml"
    else:
        candidate = Path(env.get("XDG_CONFIG_HOME", home / ".config")) / "bulkcallanalysis" / "site.toml"
    checked.append(candidate)
    if candidate.is_file():
        return candidate
    raise ConfigError("configuration not found; checked: " + ", ".join(str(item) for item in checked))
