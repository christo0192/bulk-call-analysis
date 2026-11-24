"""Deterministic structured-PII detectors shared by masking and leak scanning."""

from __future__ import annotations

import re
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
    r"(?i)\b(?:g[\s-]?mail|y\s?mail|yahoo|hot\s?mail|out\s?look|"
    r"rediff\s?mail|i\s?cloud|proton\s?mail|zoho\s?mail)\b"
    r"(?:\s*(?:\.|dot)\s*(?:com|in|co|net|org)\b)?"
)
_spoken_email = re.compile(
    r"(?ix)(?<![\w@])"
    r"(?:[a-z0-9](?:[._-]?[a-z0-9]){0,63}@|\bat(?:\s+the\s+rate(?:\s+of)?|\s+sign)?)\s*[,]*\s*"
    r"[a-z0-9](?:-?[a-z0-9]){0,63}(?:\s+[a-z0-9](?:-?[a-z0-9]){0,63})?"
    r"[,]*\s*(?:(?:dot\.?|period|point)\s*|\.\s*)"
    r"(?:[a-z]{2,10}|[a-z](?:\s+[a-z]){1,9})"
    r"(?:\s*(?:(?:dot\.?|period|point)\s*|\.\s*)[a-z0-9](?:-?[a-z0-9]){0,63}(?!\s*:))*(?=\b)"
)
_number_word = re.compile(
    r"\b(?:zero|oh|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|"
    r"fifty|sixty|seventy|eighty|ninety|double|triple|nau|aath|saat|chhe|paanch|char|teen|do|ek|shoonya)\b",
    re.I,
)
_spoken_email_stop_words = frozenset(
    {
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
        "contact",
    }
)
_spoken_email_separators = frozenset({"dot", "underscore", "hyphen", "dash"})
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
    replacements: list[tuple[int, int]] = []
    stop = _spoken_email_stop_words
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
            token_value = token.casefold()
            internal_stop = (
                token_value in stop
                and k >= 2
                and tokens[k - 2].group().strip(",.?!").casefold() in _spoken_email_separators
            )
            if len(token) > 1 and token_value in stop and not internal_stop:
                break
            if meaningful >= 6:
                break
            meaningful += len(token) > 1
            k -= 1
        if k != j:
            replacements.append((tokens[k].start(), domain.end()))

    return replacements


def _generic_spoken_email_replacements(text: str) -> list[tuple[int, int]]:
    replacements: list[tuple[int, int]] = []
    stop = _spoken_email_stop_words
    local_token = re.compile(r"(?i)[a-z0-9]+(?:[._-][a-z0-9]+)*|[._-]")
    single_words = {word.casefold() for word in _number_word.findall(text)}
    punctuation = set(",.;:!?()\"'")
    for match in _spoken_email.finditer(text):
        if "@" in match.group():
            replacements.append(match.span())
            continue
        tokens = list(re.finditer(r"\S+", text[: match.start()]))
        start = match.start()
        single_count = word_count = 0
        accepted: list[tuple[int, int, bool]] = []
        for raw_count, index in enumerate(range(len(tokens) - 1, -1, -1), 1):
            if raw_count > 24:
                break
            token = tokens[index]
            raw = token.group()
            left = len(raw) - len(raw.lstrip(" ,.;:!?()\"'"))
            right = len(raw.rstrip(" ,.;:!?()\"'"))
            value = raw[left:right]
            if not value or not local_token.fullmatch(value):
                break
            is_single = len(value) == 1 or value.casefold() in single_words
            internal_stop = (
                len(value) > 1
                and value.casefold() in stop
                and index >= 2
                and tokens[index - 1].group().strip(" ,.;:!?()\"'").casefold() in _spoken_email_separators
            )
            if len(value) > 1 and value.casefold() in stop and not internal_stop:
                break
            trailing_punctuation = raw[right:]
            if trailing_punctuation.strip(",") and not is_single:
                break
            if index < len(tokens) - 1:
                gap = text[token.end() : tokens[index + 1].start()]
                if any(char in punctuation for char in gap) and not (is_single and accepted[-1][2]):
                    break
            if value.casefold() in _spoken_email_separators:
                is_single = False
            elif is_single:
                if single_count >= 20:
                    break
                single_count += 1
            else:
                if word_count >= 6:
                    break
                word_count += 1
            accepted.append((token.start() + left, token.start() + right, is_single))
            start = token.start() + left
        if accepted:
            replacements.append((start, match.end()))
    return replacements


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


def apply_patterns(
    text: str, allow_ids: set[str] | frozenset[str] = frozenset()
) -> tuple[str, dict[str, int]]:
    counts: dict[str, int] = {}
    spans: list[tuple[int, int, str, str]] = []
    email_spans = set(_spoken_email_replacements(text) + _generic_spoken_email_replacements(text))
    for start, end in email_spans:
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
            counts[detector.name] = counts.get(detector.name, 0) + 1
            spans.append((match.start(), match.end(), detector.replacement, detector.name))
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
