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
    # One unbroken token full of provider words (each unit repeats with no space):
    # gate 9 found these quadratic in the provider floor and in apply_patterns.
    "gmail-",
    "x.gmail",
    "a'gmail",
    # Units that end on a word boundary, so every repeat is a provider match (gate 10).
    "x.gmail.",
    "a'gmail-",
    "a@gmail.com,",
    "at.gmail.at@",
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


def test_long_token_does_not_disable_provider_masking() -> None:
    prefix = "https://example.com/"
    text = prefix + "a" * (1100 - len(prefix)) + " my id is ravi-gmail and ravi98 at-gmail dot com"

    masked = mask_text(text, MaskingContext()).text

    assert "ravi" not in masked
    assert "ravi98" not in masked


@pytest.mark.timeout(240)
@pytest.mark.parametrize("call", [_mask, _scan], ids=["mask", "scan"])
@pytest.mark.parametrize(
    "build",
    [
        lambda size: "x" + "." * (size // 2) + " " + ("gmail-" * (size // 12 + 1))[: size // 2],
        lambda size: "word" + "!" * (size // 2) + " " + ("yahoo." * (size // 12 + 1))[: size // 2],
        lambda size: "a " * (size // 4) + ("gmail-" * (size // 12 + 1))[: size // 2],
    ],
    ids=["dots-then-gmail", "bangs-then-yahoo", "one-char-words"],
)
def test_mask_and_scan_are_linear_after_long_runs(
    build: Callable[[int], str], call: Callable[[str], None]
) -> None:
    # Gate 10: a long punctuation run before many matches made the floor quadratic.
    def best(size: int) -> float:
        fastest = float("inf")
        for attempt in range(3):
            text = build(size) + f" end{attempt}"
            started = time.perf_counter()
            call(text)
            fastest = min(fastest, time.perf_counter() - started)
        return fastest

    small = best(SMALL)
    large = best(LARGE)
    assert large < CEILING_SECONDS
    assert large < MAX_GROWTH * max(small, 0.02)


def test_long_link_keeps_parser_provider_forms() -> None:
    # One long link in the field used to switch off the parser's provider forms for all of it.
    link = "https://www.google.com/url?q=https://zoom.us/rec/share/" + "aB3-x.Yz_9" * 110 + "&sa=D"
    text = (
        f"Advisor: I pasted the recording link {link} in chat.\n"
        "Lead: okay and my email is anil99 at g maildot com"
    )

    masked = mask_text(text, MaskingContext()).text

    assert "anil99" not in masked
    assert "[EMAIL]" in masked
