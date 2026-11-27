from __future__ import annotations

import logging
import os
import time
from collections.abc import Generator
from pathlib import Path

import pytest


def pytest_configure(config: pytest.Config) -> None:
    root = os.environ.get("BCA_PYTEST_BASETEMP_ROOT")
    if root and config.option.basetemp is None:
        base_root = Path(root)
        base_root.mkdir(parents=True, exist_ok=True)
        config.option.basetemp = base_root / f"run-{os.getpid()}-{time.monotonic_ns()}"


@pytest.fixture(autouse=True)
def no_real_claude(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No test runs the Claude Code installed on the machine: without this, a backend built with no
    BCA_CLAUDE_BIN would link and start the developer's real program. A test that needs a program
    sets BCA_CLAUDE_BIN itself (or removes it, as the pinned-copy tests do with a fake home)."""
    monkeypatch.setenv("BCA_CLAUDE_BIN", str(tmp_path / "no-real-claude" / "claude.exe"))


@pytest.fixture(autouse=True)
def clean_bca_logging() -> Generator[None, None, None]:
    yield
    logger = logging.getLogger("bca")
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    logger.filters.clear()
