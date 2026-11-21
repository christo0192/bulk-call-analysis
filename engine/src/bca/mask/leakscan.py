"""Independent, configurable post-mask leak scanning."""

from __future__ import annotations

import gzip
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .fields import free_text_values
from .patterns import (
    _aadhaar,
    _card,
    _digits,
    _email,
    _money_spans,
    _number_word_spans,
    _pan,
    _phone,
    _spelled_pan,
    _spoken_email_replacements,
    _upi,
    _url,
)


@dataclass(frozen=True)
class Hit:
    detector: str
    start: int
    end: int
    precision: str = "high"


@dataclass(frozen=True)
class LeakReport:
    hits: list[Hit]

    @property
    def held_back(self) -> bool:
        return any(hit.precision == "high" for hit in self.hits)


_detectors: dict[str, tuple[re.Pattern[str], str]] = {
    "PHONE_LIKE": (_phone, "high"),
    "EMAIL_LIKE": (_email, "high"),
    "PAN_LIKE": (_pan, "high"),
    "AADHAAR_LIKE": (_aadhaar, "high"),
    "CARD_LIKE": (_card, "high"),
    "DIGITS_LIKE": (_digits, "high"),
    "URL_LIKE": (_url, "high"),
    "UPI_LIKE": (_upi, "high"),
    "HANDLE": (re.compile(r"(?<!\w)@[A-Za-z0-9_]{3,}"), "medium"),
}
_placeholder = re.compile(r"\[[A-Z_]+\]")


def scan_text(
    text: str, allow_ids: frozenset[str] = frozenset(), precision: frozenset[str] | None = None
) -> list[Hit]:
    hits: list[Hit] = []
    for name, (regex, detector_precision) in _detectors.items():
        if precision is not None and detector_precision not in precision:
            continue
        for match in regex.finditer(text):
            value = match.group()
            if value in allow_ids or _placeholder.fullmatch(value):
                continue
            if name == "DIGITS_LIKE" and any(
                match.start() < end and match.end() > start for start, end in _money_spans(text)
            ):
                continue
            hits.append(Hit(name, match.start(), match.end(), detector_precision))
    if precision is None or "high" in precision:
        for match in _spelled_pan.finditer(text):
            if match.group() not in allow_ids:
                hits.append(Hit("PAN_LIKE", match.start(), match.end()))
    if precision is None or "high" in precision:
        for start, end in _number_word_spans(text):
            hits.append(Hit("NUMBER_WORDS", start, end))
        for start, end in _spoken_email_replacements(text):
            hits.append(Hit("EMAIL_LIKE", start, end))
    return sorted(hits, key=lambda hit: (hit.start, hit.end, hit.detector))


def scan_lead(
    masked_file: Path, allow_ids: frozenset[str] = frozenset(), precision: frozenset[str] | None = None
) -> LeakReport:
    hits: list[Hit] = []
    with gzip.open(masked_file, "rt", encoding="utf-8") as source:
        for line in source:
            record = json.loads(line)
            for text in free_text_values(record):
                hits.extend(scan_text(text, allow_ids, precision))
    return LeakReport(hits)


def calibrate(files: Iterable[Path], allow_ids: frozenset[str] = frozenset()) -> dict[str, int]:
    totals: dict[str, int] = {}
    for path in files:
        for hit in scan_lead(path, allow_ids).hits:
            totals[hit.detector] = totals.get(hit.detector, 0) + 1
    return totals
