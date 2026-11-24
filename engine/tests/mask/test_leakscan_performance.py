import time

import pytest

from bca.mask.leakscan import scan_text
from bca.mask.masker import MaskingContext, mask_text

PERFORMANCE_INPUTS = (
    "x" * 200_000,
    " at " * 50_000,
    " dot " * 40_000,
    "a " * 100_000,
    "7 " * 100_000,
    "I am working at Infosys. My salary is low. Send it to gmail. " * 4_000,
    "a." * 100_000,
)


@pytest.mark.parametrize("text", PERFORMANCE_INPUTS)
def test_mask_and_scan_are_fast_on_200k_inputs(text: str) -> None:
    assert len(text) >= 200_000
    started = time.perf_counter()
    mask_text(text, MaskingContext())
    mask_elapsed = time.perf_counter() - started
    started = time.perf_counter()
    scan_text(text)
    scan_elapsed = time.perf_counter() - started
    assert mask_elapsed < 1.0
    assert scan_elapsed < 1.0


def test_leak_scan_is_linear_on_a_long_token() -> None:
    text = "x" * 200_000 + "lead1"

    started = time.perf_counter()
    scan_text(text)
    elapsed = time.perf_counter() - started

    assert elapsed < 1.0


def test_masker_is_linear_on_a_long_token() -> None:
    text = "x" * 200_000 + "lead1"

    started = time.perf_counter()
    mask_text(text, MaskingContext())
    elapsed = time.perf_counter() - started

    assert elapsed < 1.0
