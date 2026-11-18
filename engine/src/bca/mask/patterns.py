"""Deterministic, money-preserving structured-PII detectors."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Detector:
    name: str
    regex: re.Pattern[str]
    replacement: str


_money = re.compile(
    r"(?i)(?:₹|rs\.?|inr)\s*\d[\d,.]*(?:\s*(?:k|thousand|lakh|lakhs|lac|lacs))?|\b\d[\d,.]*\s*(?:k|thousand|lakh|lakhs|lac|lacs)\b"
)
_phone = re.compile(r"(?<!\w)(?:\+?\d{1,3}[ .-]?)?[6-9]\d{4}[ .-]?\d{5}(?!\w)")
_email = re.compile(
    r"(?i)\b(?:[\w.+-]+@[\w-]+(?:\.[\w-]+)+|[a-z]+(?:\s+dot\s+[a-z]+)?\s+at\s+(?:gmail|yahoo|hotmail|outlook)\s+dot\s+(?:com|in|net|org)|(?:[a-z]\s+){2,}[a-z]\s+at\s+(?:gmail|yahoo|hotmail|outlook)\s+dot\s+(?:com|in|net|org))\b"
)
_pan = re.compile(r"(?i)\b[a-z]{5}[ -]?\d{4}[ -]?[a-z]\b")
_aadhaar = re.compile(r"(?<!\d)(?:\d{4}[ -]?){2}\d{4}(?!\d)")
_card = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_digits = re.compile(r"(?<![\d\w])\d(?:[\d, -]*\d){5,}(?![\d\w])")
_url = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>]+")
_digit_words = re.compile(
    r"(?i)\b(?:(?:double|triple)\s+)?(?:zero|oh|one|two|three|four|five|six|seven|eight|nine)(?:\s+(?:(?:double|triple)\s+)?(?:zero|oh|one|two|three|four|five|six|seven|eight|nine)){5,}\b"
)
_spelled_digits = re.compile(r"(?<![\w])(?:\d\s+){5,}\d(?![\w])")

DETECTORS: tuple[Detector, ...] = (
    Detector("URL", _url, "[URL]"),
    Detector("EMAIL", _email, "[EMAIL]"),
    Detector("PHONE", _phone, "[PHONE]"),
    Detector("PAN", _pan, "[PAN]"),
    Detector("CARD", _card, "[CARD]"),
    Detector("AADHAAR", _aadhaar, "[AADHAAR]"),
    Detector("DIGITS", _spelled_digits, "[DIGITS]"),
    Detector("DIGITS", _digit_words, "[DIGITS]"),
    Detector("DIGITS", _digits, "[DIGITS]"),
)


def _money_spans(text: str) -> list[tuple[int, int]]:
    return [match.span() for match in _money.finditer(text)]


def _replacement(
    detector: Detector,
    protected: list[tuple[int, int]],
    allow_ids: set[str] | frozenset[str],
    counts: dict[str, int],
) -> Callable[[re.Match[str]], str]:
    def replace(match: re.Match[str]) -> str:
        value = match.group()
        if value.strip() in allow_ids or any(
            match.start() < end and match.end() > start for start, end in protected
        ):
            return value
        counts[detector.name] = counts.get(detector.name, 0) + 1
        return detector.replacement

    return replace


def apply_patterns(
    text: str, allow_ids: set[str] | frozenset[str] = frozenset()
) -> tuple[str, dict[str, int]]:
    counts: dict[str, int] = {}
    current = text
    for detector in DETECTORS:
        protected = _money_spans(current)
        current = detector.regex.sub(_replacement(detector, protected, allow_ids, counts), current)
    return current, counts
