from __future__ import annotations

import json
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from bca.logsetup import setup_logging


def test_json_and_redaction(tmp_path: Path) -> None:
    logger = setup_logging(tmp_path)
    api_key = "sk-" + "a" * 24
    aws_key = "AKIA" + "A" * 16
    logger.info("Bearer %s", api_key, extra={"token": aws_key})
    for handler in logger.handlers:
        handler.flush()
    line = json.loads((tmp_path / "engine.log").read_text().splitlines()[0])
    assert "secret" not in line["msg"] and "[REDACTED]" in line["msg"]
    assert line["token"] == "[REDACTED]"


def test_formatting_and_nested_extra_redaction(tmp_path: Path) -> None:
    logger = setup_logging(tmp_path)
    logger.info("processed %d leads", 5)
    logger.info("%(n)s leads", {"n": 3})
    logger.info("headers", extra={"headers": {"Authorization": "Bearer " + "x" * 20}})
    for handler in logger.handlers:
        handler.flush()
    lines = [json.loads(line) for line in (tmp_path / "engine.log").read_text().splitlines()]
    assert [line["msg"] for line in lines[:2]] == ["processed 5 leads", "3 leads"]
    assert lines[2]["headers"]["Authorization"] == "Bearer [REDACTED]"


def test_exception_and_stack_are_redacted(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    logger = setup_logging(tmp_path)
    token = "sk-" + "b" * 24
    try:
        raise RuntimeError("failure " + token)
    except RuntimeError:
        logger.exception("operation failed")
    for handler in logger.handlers:
        handler.flush()
    captured = capsys.readouterr()
    line = json.loads((tmp_path / "engine.log").read_text().splitlines()[0])
    assert "exc" in line and token not in line["exc"]
    assert token not in captured.err


def test_invalid_level_and_unrelated_handlers(tmp_path: Path) -> None:
    logger = logging.getLogger("bca")
    other = logging.StreamHandler(sys.stdout)
    marker = logging.Filter()
    logger.addHandler(other)
    logger.addFilter(marker)
    try:
        with pytest.raises(ValueError):
            setup_logging(tmp_path, "VERBOSE")
        setup_logging(tmp_path)
        assert other in logger.handlers
        assert marker in logger.filters
    finally:
        logger.removeHandler(other)
        other.close()
        logger.removeFilter(marker)


def test_all_redaction_patterns(tmp_path: Path) -> None:
    logger = setup_logging(tmp_path)
    github_token = "ghp_" + "a" * 24
    api_key = "sk-" + "a" * 24
    aws_key = "AKIA" + "A" * 16
    pem = (
        "-" * 5
        + "BEGIN RSA "
        + "PRIVATE KEY"
        + "-" * 5
        + "x"
        + "-" * 5
        + "END RSA "
        + "PRIVATE KEY"
        + "-" * 5
    )
    private_key = '"' + "private_" + "key" + '":"secret"'
    logger.warning(f"{github_token} {api_key} {aws_key} {pem} {private_key}")
    for handler in logger.handlers:
        handler.flush()
    lines = [json.loads(line) for line in (tmp_path / "engine.log").read_text().splitlines()]
    message = lines[0]["msg"]
    assert github_token not in message
    assert api_key not in message
    assert aws_key not in message
    assert "PRIVATE KEY-----x" not in message
    assert private_key not in message
    assert "[REDACTED]" in message


def test_no_duplicate_handlers(tmp_path: Path) -> None:
    first = setup_logging(tmp_path)
    pytest_handlers = [handler for handler in first.handlers if not getattr(handler, "_bca_installed", False)]
    second = setup_logging(tmp_path)
    assert first is second
    installed = [handler for handler in first.handlers if getattr(handler, "_bca_installed", False)]
    assert len(installed) == 2
    assert sum(isinstance(handler, RotatingFileHandler) for handler in installed) == 1
    assert sum(type(handler) is logging.StreamHandler for handler in installed) == 1
    remaining = [handler for handler in first.handlers if not getattr(handler, "_bca_installed", False)]
    assert remaining == pytest_handlers
    for handler in installed:
        first.removeHandler(handler)
        handler.close()


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
