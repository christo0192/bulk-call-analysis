from __future__ import annotations

import time

import pytest

from bca.mask.masker import MaskingContext, mask_text


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("my email is ravi kumar at the rate gmail dot com", "my email is [EMAIL]"),
        ("id is ravikumar98 at the rate of gmail dot com", "id is [EMAIL]"),
        ("ravi.kumar at the rate gmail.com", "[EMAIL]"),
        ("ravi kumar at g mail dot com", "[EMAIL]"),
        ("nine-eight-seven-six-five-four-three-two-one-zero", "[DIGITS]"),
        ("double nine double eight double seven double six double five", "[DIGITS]"),
        ("my number is 9. 8. 7. 6. 5. 4. 3. 2. 1. 0.", "my number is [DIGITS]."),
        ("aadhaar 1234/5678/9012", "aadhaar [AADHAAR]"),
        ("ok sir yours 98765.43210 right", "ok sir yours [PHONE] right"),
        ("the fee is 2,50,000 rupees", "the fee is 2,50,000 rupees"),
        ("since 2019 I have been at the bank", "since 2019 I have been at the bank"),
        ("about 5000 a month extra", "about 5000 a month extra"),
        ("this is too expensive.", "this is too expensive."),
        ("This is a good offer.", "This is a good offer."),
        ("Myself Ravi Kumar from Pune.", "Myself [NAME] from Pune."),
        ("Arjun calling from the admissions team", "[NAME] calling from the admissions team"),
        ("PAN abcde 12 34 f", "[PAN]"),
        ("my profile is linkedin.com/in/ravi-kumar-12ab", "my profile is [URL]"),
        ("send to ravikumar@okaxis", "send to [UPI]"),
    ],
)
def test_review_cases_have_exact_outputs(source: str, expected: str) -> None:
    assert mask_text(source, MaskingContext()).text == expected


def test_digits_regex_is_linear_on_a_long_run() -> None:
    started = time.monotonic()
    mask_text("x " + "1" * 40 + "a", MaskingContext())
    assert time.monotonic() - started < 0.5
