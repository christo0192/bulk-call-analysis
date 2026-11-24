"""Deterministic structured-PII detectors shared by masking and leak scanning."""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import main_provider_rule, spoken_email


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
_email = re.compile(r"(?i)(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_pan = re.compile(r"(?i)\b[a-z]{5}\d{4}[a-z]\b")
_pan_context = re.compile(r"(?i)\bpan\s+[a-z]{5}[ -]?(?:\d[ -]?){4}[a-z]\b")
_aadhaar = re.compile(r"(?<!\d)(?:\d{4}[ ./-]?){2}\d{4}(?!\d)")
_card = re.compile(r"(?<!\d)(?:\d[ -.]?){13,19}(?!\d)")
_digits = re.compile(r"(?<![\d\w])\d(?:(?:[,./-]\s*|\s+)?\d){5,}(?![\d\w])")
_url = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>]+|\b(?:linkedin|github|instagram)\.com/[^\s<>]+")
_upi = re.compile(r"(?i)(?<![\w.+-])[\w.-]+@(?:okaxis|oksbi|okhdfcbank|ybl|paytm|upi|ibl|icici)\b")
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

# Higher-priority spans win when different detectors cover the same original text.
_DETECTOR_PRIORITY = {
    "EMAIL": 60,
    "PHONE": 50,
    "AADHAAR": 40,
    "PAN": 30,
    "CARD": 20,
}


def _money_spans(text: str) -> list[tuple[int, int]]:
    spans = [m.span() for m in _money.finditer(text)]
    spans.extend(m.span() for m in _dates.finditer(text))
    spans.extend(m.span() for m in _years.finditer(text))
    spans.extend(m.span() for m in _scores.finditer(text))
    return spans


def _spoken_email_replacements(text: str) -> list[tuple[int, int]]:
    """Compatibility hook retained for callers that monkeypatch this name."""
    return spoken_email.provider_replacements(text)


def _number_word_spans(text: str) -> list[tuple[int, int]]:
    if not _number_word.search(text):
        return []
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


def apply_patterns(
    text: str, allow_ids: set[str] | frozenset[str] = frozenset()
) -> tuple[str, dict[str, int]]:
    counts: dict[str, int] = {}
    spans: list[tuple[int, int, str, str]] = []
    email_spans = set(
        _spoken_email_replacements(text)
        + main_provider_rule.provider_replacements(text)
        + spoken_email.find_spoken_emails(text)
    )
    for start, end in email_spans:
        if text[start:end] in allow_ids:
            continue
        while start < end and text[start] in "(\"'":
            start += 1
        counts["EMAIL"] = counts.get("EMAIL", 0) + 1
        spans.append((start, end, "[EMAIL]", "EMAIL"))
    enclosing = [
        match.span()
        for detector in DETECTORS
        if detector.name in {"EMAIL", "URL", "UPI"}
        for match in detector.regex.finditer(text)
    ]
    protected = [
        span
        for span in _money_spans(text)
        if not any(
            enclosing_start <= span[0] and span[1] <= enclosing_end
            for enclosing_start, enclosing_end in enclosing
        )
    ]
    structured = _money_spans(text) + [
        match.span()
        for detector in (
            Detector("PHONE", _phone, ""),
            Detector("CARD", _card, ""),
            Detector("AADHAAR", _aadhaar, ""),
        )
        for match in detector.regex.finditer(text)
    ]
    for start, end in _number_word_spans(text):
        if text[start:end].strip() in allow_ids or any(
            start < protected_end and end > protected_start for protected_start, protected_end in structured
        ):
            continue
        counts["DIGITS"] = counts.get("DIGITS", 0) + 1
        spans.append((start, end, "[DIGITS]", "DIGITS"))
    for detector in DETECTORS:
        for match in detector.regex.finditer(text):
            if match.group().strip() in allow_ids or any(
                match.start() < end and match.end() > start for start, end in protected
            ):
                continue
            start = match.start()
            if detector.name == "URL" and email_spans:
                prefix = re.search(r"(?i)\blinkedin\s+is\s*$", text[:start])
                if prefix:
                    start = prefix.start()
            counts[detector.name] = counts.get(detector.name, 0) + 1
            spans.append((start, match.end(), detector.replacement, detector.name))
    merged: list[tuple[int, int, str, str, int]] = []
    for start, end, replacement, detector_name in sorted(spans):
        if merged and start <= merged[-1][1]:
            old_start, old_end, old_replacement, old_detector, old_length = merged[-1]
            length = end - start
            if length > old_length or (
                length == old_length
                and _DETECTOR_PRIORITY.get(detector_name, 0) > _DETECTOR_PRIORITY.get(old_detector, 0)
            ):
                old_replacement, old_detector = replacement, detector_name
                old_length = length
            merged[-1] = (old_start, max(old_end, end), old_replacement, old_detector, old_length)
        else:
            merged.append((start, end, replacement, detector_name, end - start))
    current = text
    for start, end, replacement, _, _ in reversed(merged):
        current = current[:start] + replacement + current[end:]
    return current, counts
