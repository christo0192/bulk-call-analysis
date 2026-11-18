from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from bca.logsetup import setup_logging


def test_json_and_redaction(tmp_path: Path) -> None:
    logger = setup_logging(tmp_path)
    api_key = "sk-" + "a" * 24
    aws_key = "AKIA" + "A" * 16
    logger.info(f"Bearer secret {api_key}", extra={"token": aws_key})
    for handler in logger.handlers:
        handler.flush()
    line = json.loads((tmp_path / "engine.log").read_text().splitlines()[0])
    assert "secret" not in line["msg"] and "[REDACTED]" in line["msg"]
    assert line["token"] == "[REDACTED]"


def test_all_redaction_patterns(tmp_path: Path) -> None:
    logger = setup_logging(tmp_path)
    github_token = "ghp_" + "a" * 24
    api_key = "sk-" + "a" * 24
    aws_key = "AKIA" + "A" * 16
    pem = "-----BEGIN RSA PRIVATE KEY-----x-----END RSA PRIVATE KEY-----"
    private_key = '"' + "private_" + "key" + '":"secret"'
    logger.warning(f"{github_token} {api_key} {aws_key} {pem} {private_key}")
    for handler in logger.handlers:
        handler.flush()
    text = (tmp_path / "engine.log").read_text()
    assert "abcdefghijklmnopqrstuvwxyz" not in text
    assert "PRIVATE KEY-----x" not in text
    assert '"private_key":"secret"' not in text


def test_no_duplicate_handlers(tmp_path: Path) -> None:
    first = setup_logging(tmp_path)
    second = setup_logging(tmp_path)
    assert first is second
    assert len(first.handlers) == 2
    for handler in first.handlers:
        handler.close()
    first.handlers.clear()


def test_replaces_handlers_for_new_log_directory(tmp_path: Path) -> None:
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    logger = setup_logging(first_dir)
    setup_logging(second_dir)

    logger.info("written to the second directory")
    for handler in logger.handlers:
        handler.flush()

    assert not (first_dir / "engine.log").exists() or (first_dir / "engine.log").read_text() == ""
    assert "written to the second directory" in (second_dir / "engine.log").read_text()
    assert sum(isinstance(handler, RotatingFileHandler) for handler in logger.handlers) == 1
    assert sum(type(handler) is logging.StreamHandler for handler in logger.handlers) == 1
