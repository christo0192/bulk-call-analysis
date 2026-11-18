from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, cast

_SECRET_PATTERNS = [
    (re.compile(r"Bearer\s+\S+", re.IGNORECASE), "Bearer [REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]+"), "[REDACTED]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "[REDACTED]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}"), "[REDACTED]"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED]"),
    (
        re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|\Z)", re.DOTALL),
        "[REDACTED PEM]",
    ),
    (re.compile(r'("private_key"\s*:\s*")[^"]*(")', re.IGNORECASE), r"\1[REDACTED]\2"),
]
_STANDARD_FIELDS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__)
_CORE_FIELDS = {"ts", "level", "logger", "msg"}


def _redact(value: str) -> str:
    for pattern, replacement in _SECRET_PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def _redact_nested(value: Any) -> Any:
    if isinstance(value, str):
        return _redact(value)
    if isinstance(value, dict):
        return {_redact_nested(key): _redact_nested(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_nested(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_nested(item) for item in value)
    if isinstance(value, set):
        return {_redact_nested(item) for item in value}
    return value


class RedactingFilter(logging.Filter):
    _bca_installed: bool

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _redact(record.getMessage())
        record.args = None
        if record.exc_info:
            record.exc_text = _redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        if record.stack_info:
            record.stack_info = _redact(record.stack_info)
        for key, value in list(record.__dict__.items()):
            if key not in _STANDARD_FIELDS:
                setattr(record, key, _redact_nested(value))
        return True


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        fields: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_text:
            fields["exc"] = record.exc_text
        if record.stack_info:
            fields["stack"] = record.stack_info
        for key, value in record.__dict__.items():
            if key not in _STANDARD_FIELDS and not key.startswith("_"):
                try:
                    json.dumps(value)
                except (TypeError, ValueError):
                    value = str(value)
                fields[f"extra_{key}" if key in _CORE_FIELDS else key] = value
        return json.dumps(fields, ensure_ascii=False)


class _ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        formatted = super().format(record)
        if record.exc_text and record.exc_text not in formatted:
            formatted += "\n" + record.exc_text
        return formatted


def setup_logging(log_dir: Path, level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger("bca")
    level_number = logging.getLevelName(level.upper())
    if not isinstance(level_number, int):
        raise ValueError(f"invalid log level: {level}")
    logger.setLevel(level_number)
    logger.propagate = False
    for handler in logger.handlers[:]:
        if getattr(handler, "_bca_installed", False):
            logger.removeHandler(handler)
            handler.close()
    for installed_filter in logger.filters[:]:
        if getattr(installed_filter, "_bca_installed", False):
            logger.removeFilter(installed_filter)

    log_dir.mkdir(parents=True, exist_ok=True)
    redactor = RedactingFilter()
    redactor._bca_installed = True
    logger.addFilter(redactor)
    file_handler = RotatingFileHandler(
        log_dir / "engine.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    cast(Any, file_handler)._bca_installed = True
    file_handler.setFormatter(_JsonFormatter())
    file_handler.addFilter(redactor)
    console = logging.StreamHandler()
    cast(Any, console)._bca_installed = True
    console.setFormatter(_ConsoleFormatter("%(levelname)s %(name)s: %(message)s"))
    console.addFilter(redactor)
    logger.addHandler(file_handler)
    logger.addHandler(console)
    return logger
