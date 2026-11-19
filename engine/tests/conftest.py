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
def clean_bca_logging() -> Generator[None, None, None]:
    yield
    logger = logging.getLogger("bca")
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    logger.filters.clear()
