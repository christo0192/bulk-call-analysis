import time

from bca.mask.leakscan import scan_text
from bca.mask.masker import MaskingContext, mask_text


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
