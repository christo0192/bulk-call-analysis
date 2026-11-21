"""Deterministic structured-PII detectors shared by masking and leak scanning."""

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
    r"(?ix)(?<!\w)(?:₹|â‚¹|rs\.?|inr)\s*\d[\d,.]*(?:\s*(?:k|thousand|lakh|lakhs|lac|lacs))?\b"  # noqa: RUF001
    r"|(?<!\w)\d{1,2}(?:,\d{2})+,\d{3}(?:\s+(?:rupees?|rs\.?|inr|lpa)\b)?"
    r"|(?<!\w)\d[\d,.]*(?:\s+(?:rupees?|rs\.?|inr|lpa|per\s+(?:month|annum))\b)"
    r"|\b(?:paid|fee|cost|amount|price)\s+\d{4,7}\b"
    r"|\b\d{4,7}\s+(?:a|per)\s+month\b"
    r"|(?<!\w)\d[\d,.]*\s*(?:k|thousand|lakh|lakhs|lac|lacs)\b"
)
_phone = re.compile(r"(?<!\w)(?:\+?\d{1,3}[ .-]?)?[6-9]\d{4}[ .-]?\d{5}(?!\w)")
_email = re.compile(r"(?i)\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_pan = re.compile(r"(?i)\b[a-z]{5}\d{4}[a-z]\b")
_pan_context = re.compile(r"(?i)\bpan\s+[a-z]{5}[ -]?(?:\d[ -]?){4}[a-z]\b")
_aadhaar = re.compile(r"(?<!\d)(?:\d{4}[ ./-]?){2}\d{4}(?!\d)")
_card = re.compile(r"(?<!\d)(?:\d[ -.]?){13,19}(?!\d)")
_digits = re.compile(r"(?<![\d\w])\d(?:(?:[,./-]\s*|\s+)?\d){5,}(?![\d\w])")
_url = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>]+|\b(?:linkedin|github|instagram)\.com/[^\s<>]+")
_upi = re.compile(r"(?i)(?<!\w)[\w.-]+@(?:okaxis|oksbi|okhdfcbank|ybl|paytm|upi|ibl|icici)\b")
_spoken_domain = re.compile(
    r"(?i)\b(?:g\s?mail|yahoo|ymail|hotmail|outlook|rediff(?:mail)?|icloud|protonmail)\b"
    r"(?:\s*(?:\.|dot)\s*(?:com|in|co|net|org)\b)?"
)
_spoken_email = re.compile(
    r"(?ix)(?<![\w@])"
    r"[a-z0-9]+(?:(?:[._]|\s+(?:dot|underscore)\s+)[a-z0-9]+)*"
    r"\s*(?:at|@)\s*"
    r"[a-z0-9]+(?:(?:\s*(?:\.|dot)\s*)[a-z0-9]+)*"
    r"\s*(?:\.|dot)\s*(?:com|in|org|net|co|io|ai|edu|gov|us|uk)"
    r"(?=\s|$|[,.!?])"
)
_number_word = re.compile(
    r"\b(?:zero|oh|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|"
    r"fifty|sixty|seventy|eighty|ninety|double|triple|nau|aath|saat|chhe|paanch|char|teen|do|ek|shoonya)\b",
    re.I,
)
_spelled_digits = re.compile(r"(?<![\w])(?:\d\s+){5,}\d(?![\w])")
_spelled_pan = re.compile(r"(?ix)\bpan\s+(?:[a-z]\s+){4}[a-z]\s+(?:\d\s*){4}[a-z](?!\w)")
_years = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
_dates = re.compile(r"(?<!\d)\d{1,2}[./-]\d{1,2}[./-](?:19|20)\d{2}(?!\d)")
_scores = re.compile(r"(?<!\w)\d{1,3}(?:,\s*\d{1,3}){2,}(?!\w)")

DETECTORS: tuple[Detector, ...] = (
    Detector("URL", _url, "[URL]"),
    Detector("EMAIL", _email, "[EMAIL]"),
    Detector("UPI", _upi, "[UPI]"),
    Detector("PHONE", _phone, "[PHONE]"),
    Detector("PAN", _pan, "[PAN]"),
    Detector("PAN", _pan_context, "[PAN]"),
    Detector("PAN", _spelled_pan, "[PAN]"),
    Detector("CARD", _card, "[CARD]"),
    Detector("AADHAAR", _aadhaar, "[AADHAAR]"),
    Detector("DIGITS", _spelled_digits, "[DIGITS]"),
    Detector("DIGITS", _digits, "[DIGITS]"),
)


def _money_spans(text: str) -> list[tuple[int, int]]:
    spans = [m.span() for m in _money.finditer(text)]
    spans.extend(m.span() for m in _dates.finditer(text))
    spans.extend(m.span() for m in _years.finditer(text))
    spans.extend(m.span() for m in _scores.finditer(text))
    return spans


def _spoken_email_replacements(text: str) -> list[tuple[int, int]]:
    replacements = {match.span() for match in _spoken_email.finditer(text)}
    stop = {
        "is",
        "id",
        "email",
        "mail",
        "your",
        "my",
        "address",
        "that",
        "sir",
        "it",
        "to",
        "on",
        "me",
        "send",
        "sent",
        "for",
        "this",
        "and",
        "or",
        "in",
        "please",
        "yes",
        "so",
        "okay",
        "ok",
        "its",
        "it's",
        "will",
        "you",
        "have",
        "has",
        "was",
        "be",
        "check",
        "same",
        "registered",
        "official",
    }
    connectors = {"at", "the", "rate", "of", "@"}
    for domain in _spoken_domain.finditer(text):
        tokens = list(re.finditer(r"\S+", text[: domain.start()]))
        if not tokens:
            continue
        j = len(tokens)
        while j and tokens[j - 1].group().strip(",.?!").casefold() in connectors:
            j -= 1
        k, meaningful = j, 0
        while k:
            token = tokens[k - 1].group().strip(",.?!")
            if len(token) > 1 and token.casefold() in stop:
                break
            if meaningful >= 6:
                break
            meaningful += len(token) > 1
            k -= 1
        if k != j:
            replacements.add((tokens[k].start(), domain.end()))
    return sorted(replacements)


def _number_word_spans(text: str) -> list[tuple[int, int]]:
    token = re.compile(rf"(?:{_number_word.pattern}|\d+)", re.I)
    matches = list(token.finditer(text))
    spans: list[tuple[int, int]] = []
    start = last_end = -1
    count = 0
    for match in matches:
        gap = text[last_end : match.start()] if last_end >= 0 else ""
        gap_is_turn = bool(re.search(r"\b(?:lead|advisor)\s*:", gap, re.I)) and bool(
            re.fullmatch(r"[\s\w:,.!?/-]+", gap)
        )
        if last_end >= 0 and not gap_is_turn and not re.fullmatch(r"[\s,./-]*", gap):
            if count >= 6:
                spans.append((start, last_end))
            start, count = match.start(), 0
        if count == 0:
            start = match.start()
        value = match.group()
        count += len(value) if value.isdigit() else (2 if value.casefold() in {"double", "triple"} else 1)
        last_end = match.end()
    if count >= 6:
        spans.append((start, last_end))
    return spans


def _replacement(
    detector: Detector,
    protected: list[tuple[int, int]],
    allow_ids: set[str] | frozenset[str],
    counts: dict[str, int],
) -> Callable[[re.Match[str]], str]:
    def replace(match: re.Match[str]) -> str:
        if match.group().strip() in allow_ids or any(
            match.start() < end and match.end() > start for start, end in protected
        ):
            return match.group()
        counts[detector.name] = counts.get(detector.name, 0) + 1
        return detector.replacement

    return replace


def apply_patterns(
    text: str, allow_ids: set[str] | frozenset[str] = frozenset()
) -> tuple[str, dict[str, int]]:
    counts: dict[str, int] = {}
    current = text
    for start, end in reversed(_spoken_email_replacements(current)):
        current = current[:start] + "[EMAIL]" + current[end:]
        counts["EMAIL"] = counts.get("EMAIL", 0) + 1
    protected_structured = _money_spans(current) + [
        match.span()
        for detector in (
            Detector("PHONE", _phone, ""),
            Detector("CARD", _card, ""),
            Detector("AADHAAR", _aadhaar, ""),
        )
        for match in detector.regex.finditer(current)
    ]
    for start, end in reversed(_number_word_spans(current)):
        if current[start:end].strip() in allow_ids or any(
            start < protected_end and end > protected_start
            for protected_start, protected_end in protected_structured
        ):
            continue
        current = current[:start] + "[DIGITS]" + current[end:]
        counts["DIGITS"] = counts.get("DIGITS", 0) + 1
    for detector in DETECTORS:
        current = detector.regex.sub(
            _replacement(detector, _money_spans(current), allow_ids, counts), current
        )
    return current, counts
