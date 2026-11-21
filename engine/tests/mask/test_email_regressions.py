from __future__ import annotations

from bca.mask.masker import MaskingContext, mask_text


def masked(text: str) -> str:
    return mask_text(text, MaskingContext()).text


def test_main_webmail_cases_are_masked_again() -> None:
    for text in (
        "my email id is ravikumar1992 at gmail and my number you have",
        "send it to ravikumar at yahoo",
        "ravi kumar gmail dot com",
        "ravikumar1992 at gmail dotcom",
        "ravi at gmail. com",
        "ravi@gmail",
    ):
        assert "[EMAIL]" in masked(text)


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
