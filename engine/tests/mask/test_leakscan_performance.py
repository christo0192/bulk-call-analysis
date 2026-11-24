"""The masker and the leak scan must stay linear, whatever the speed of the machine.

A fixed time limit fails on a slow or busy machine and says nothing about growth, so these tests
compare two sizes: 4 times the input may take at most 8 times as long (linear is 4, quadratic is 16).
"""

import time
from collections.abc import Callable

import pytest

from bca.mask.leakscan import scan_text
from bca.mask.masker import MaskingContext, mask_text

LARGE = 200_000
SMALL = LARGE // 4
MAX_GROWTH = 8.0
# Far above any healthy run; only catches a hang.
CEILING_SECONDS = 20.0

PERFORMANCE_INPUTS = (
    "x",
    " at ",
    " dot ",
    "a ",
    "7 ",
    "I am working at Infosys. My salary is low. Send it to gmail. ",
    "a. gmail at dot ",
    "a- gmail at dot ",
    "ravi at acme dot com ",
    " @ ",
    "at the rate ",
)


def _mask(text: str) -> None:
    mask_text(text, MaskingContext())


def _scan(text: str) -> None:
    scan_text(text)


def _best_of(call: Callable[[str], None], unit: str, size: int) -> float:
    best = float("inf")
    for attempt in range(3):
        # A different tail each time, so a cached tokenisation is never measured.
        text = (unit * (size // len(unit) + 1))[:size] + f" end{attempt}"
        started = time.perf_counter()
        call(text)
        best = min(best, time.perf_counter() - started)
    return best


@pytest.mark.timeout(240)
@pytest.mark.parametrize("call", [_mask, _scan], ids=["mask", "scan"])
@pytest.mark.parametrize("unit", PERFORMANCE_INPUTS)
def test_mask_and_scan_are_linear_on_adversarial_inputs(unit: str, call: Callable[[str], None]) -> None:
    small = _best_of(call, unit, SMALL)
    large = _best_of(call, unit, LARGE)
    assert large < CEILING_SECONDS
    # The floor keeps timer noise on very fast cases from failing the ratio.
    assert large < MAX_GROWTH * max(small, 0.02)
