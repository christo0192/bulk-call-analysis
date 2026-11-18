from __future__ import annotations

import logging
from collections.abc import Generator

import pytest


@pytest.fixture(autouse=True)
def clean_bca_logging() -> Generator[None, None, None]:
    yield
    logger = logging.getLogger("bca")
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    logger.filters.clear()
