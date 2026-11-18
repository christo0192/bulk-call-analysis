from __future__ import annotations

import logging

import pytest


@pytest.fixture(autouse=True)
def clean_bca_logging() -> None:
    yield
    logger = logging.getLogger("bca")
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    logger.filters.clear()
