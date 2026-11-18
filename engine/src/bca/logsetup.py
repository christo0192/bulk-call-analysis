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
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED]"),
    (
        re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", re.DOTALL),
        "[REDACTED PEM]",
    ),
    (re.compile(r'("private_key"\s*:\s*")[^"]*(")', re.IGNORECASE), r"\1[REDACTED]\2"),
]
_STANDARD_FIELDS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__)


def _redact(value: str) -> str:
    for pattern, replacement in _SECRET_PATTERNS:
        value = pattern.sub(replacement, value)
    return value


class RedactingFilter(logging.Filter):
    _bca_installed: bool

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _redact(str(record.msg))
        if record.args:
            record.args = tuple(_redact(str(arg)) for arg in record.args)
        for key, value in list(record.__dict__.items()):
            if key not in _STANDARD_FIELDS and isinstance(value, str):
                setattr(record, key, _redact(value))
        return True


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        fields: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_FIELDS and not key.startswith("_"):
                try:
                    json.dumps(value)
                except (TypeError, ValueError):
                    value = str(value)
                fields[key] = value
        return json.dumps(fields, ensure_ascii=False)


def setup_logging(log_dir: Path, level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger("bca")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    logger.filters.clear()

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
    console.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    console.addFilter(redactor)
    logger.addHandler(file_handler)
    logger.addHandler(console)
    return logger
