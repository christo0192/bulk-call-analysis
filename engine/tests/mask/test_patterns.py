from __future__ import annotations

import pytest

from bca.mask.masker import MaskingContext, mask_text


@pytest.mark.parametrize(
    ("source", "placeholder"),
    [
        ("Call +91 98765 43210", "[PHONE]"),
        ("Call 98765-43210", "[PHONE]"),
        ("Write ravi.kumar@example.test", "[EMAIL]"),
        ("PAN ABCDE1234F", "[PAN]"),
        ("PAN abcde 1234 f", "[PAN]"),
        ("PAN abcde-1234-f", "[PAN]"),
        ("Aadhaar 1234 5678 9012", "[AADHAAR]"),
        ("Aadhaar 1234-5678-9012", "[AADHAAR]"),
        ("Card 4111 1111 1111 1111", "[CARD]"),
        ("Visit https://example.test/path", "[URL]"),
        ("digits 1234567890", "[DIGITS]"),
        ("digits 9 8 7 6 5 4 3 2 1 0", "[DIGITS]"),
        ("digits nine eight seven six five four three two one zero", "[DIGITS]"),
        ("digits double nine eight seven six five four", "[DIGITS]"),
        ("digits triple oh nine eight seven six five", "[DIGITS]"),
        ("spoken ravi dot kumar at gmail dot com", "[EMAIL]"),
    ],
)
def test_detector(source: str, placeholder: str) -> None:
    assert placeholder in mask_text(source, MaskingContext()).text


def test_longest_span_decides_the_placeholder() -> None:
    assert mask_text("Card 4111 1111 1111 1111", MaskingContext()).text == "Card [CARD]"


def test_local_part_with_internal_dots_is_fully_masked() -> None:
    assert mask_text("ravi.kumar at the rate gmail.com", MaskingContext()).text == "[EMAIL]"


def test_split_webmail_domain_is_masked() -> None:
    for provider in ("g mail", "hot mail", "yahoo mail"):
        assert mask_text(f"ravi kumar at {provider} dot com", MaskingContext()).text == "[EMAIL]"


@pytest.mark.parametrize("money", ["Rs 30,000", "₹ 45k", "30 thousand", "1.2 lakh", "50k"])
def test_money_survives(money: str) -> None:
    assert money in mask_text(f"The fee is {money} today", MaskingContext()).text


def test_short_digits_and_allowed_ids_survive() -> None:
    ctx = MaskingContext(allow_ids=frozenset({"123456789"}))
    assert "12345" in mask_text("12345", ctx).text
    assert "123456789" in mask_text("123456789", ctx).text


def test_plain_gmail_mention_survives() -> None:
    assert "gmail" in mask_text("Please check Gmail", MaskingContext()).text.lower()


@pytest.mark.parametrize(
    "source",
    [
        "phone 12345",
        "email name@example",
        "PAN ABCDE1234",
        "digits nine eight seven six five",
        "site example.test",
    ],
)
def test_detector_negative_cases(source: str) -> None:
    assert "[" not in mask_text(source, MaskingContext()).text
