from __future__ import annotations

from bca.mask.masker import MaskingContext, mask_text


def masked(text: str) -> str:
    return mask_text(text, MaskingContext()).text


def test_main_webmail_cases_are_masked_again() -> None:
    for text, expected in (
        (
            "my email id is ravikumar1992 at gmail and my number you have",
            "my email id is [EMAIL] and my number you have",
        ),
        ("send it to ravikumar at yahoo", "send it to [EMAIL]"),
        ("ravi kumar gmail dot com", "[EMAIL]"),
        ("ravikumar1992 at gmail dotcom", "[EMAIL]"),
        ("ravi at gmail. com", "[EMAIL]"),
        ("ravi@gmail", "[EMAIL]"),
    ):
        assert masked(text) == expected


def test_split_provider_names_are_masked() -> None:
    for text in (
        "my email is ravi kumar at hot mail",
        "my email is ravi kumar at out look",
        "my email is ravi kumar at proton mail",
        "my email is ravi kumar at i cloud",
        "my email is ravi kumar at y mail",
        "my email is ravi kumar at g-mail",
        "my email is ravi kumar at rediff mail",
        "my email is ravi kumar at zoho mail",
    ):
        assert masked(text) == "my email is [EMAIL]"


def test_odd_tld_endings_are_masked() -> None:
    for text in (
        "ravi at acme dotcom",
        "ravi@acme. com",
        "ravi at acme .com",
        "ravi at acme period com",
        "ravi kumar, at the rate, acme dot com",
        "ravi at acme dot c o m",
    ):
        assert masked(text) == "[EMAIL]"


def test_long_spoken_local_parts_are_fully_masked() -> None:
    assert masked("my email is ravi dot kumar dot sharma dot 1992 at gmail dot com") == "my email is [EMAIL]"
    assert masked("r a v i k u m a r s h a r m a at gmail dot com") == "[EMAIL]"


def test_spelled_and_bracketed_local_parts_are_fully_masked() -> None:
    for text, expected in (
        ("R. A. V. I. at gmail dot com", "[EMAIL]"),
        ("R, A, V, I, K, U, M, A, R at the rate gmail dot com", "[EMAIL]"),
        ("s h a r m a r a v i at gmail dot com", "[EMAIL]"),
        ("(ravikumar1992 at gmail dot com)", "([EMAIL])"),
        ("ravi kumar zero five one nine nine two at gmail dot com", "[EMAIL]"),
    ):
        assert masked(text) == expected


def test_birth_year_inside_written_email_is_masked() -> None:
    for text in ("ravi1992@gmail.com", "ravi.kumar.1992@gmail.com"):
        assert masked(text) == "[EMAIL]"
