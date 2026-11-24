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
    _upi,
    _url,
)
from .spoken_email import DOMAIN_STOPS, FILE_ENDINGS, KNOWN_TLDS, PROVIDERS, SPLIT_PROVIDERS, UPI_HANDLES


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


def _scan_spoken_email_tokens(text: str) -> list[Hit]:
    scrubbed = _placeholder.sub(lambda match: " " * len(match.group()), text)
    tokens = list(re.finditer(r"\S+", scrubbed))
    hits: list[Hit] = []
    for i, match in enumerate(tokens):
        raw = match.group().strip(",.;:!?)]}" + "'")
        lower = raw.casefold()
        if "@" in raw:
            left, right = raw.split("@", 1)
            if (
                left
                and right
                and any(c.isalpha() for c in left)
                and any(c.isalpha() for c in right)
                and right.casefold() not in UPI_HANDLES
            ):
                hits.append(Hit("EMAIL_LIKE", match.start(), match.end()))
                continue
            if not left and right and any(c.isalpha() for c in right) and right.casefold() not in UPI_HANDLES:
                start = i
                if i > 0 and tokens[i - 1].group().strip(",.;:!?)]}" + "'").casefold() not in DOMAIN_STOPS:
                    hits.append(Hit("EMAIL_LIKE", tokens[i - 1].start(), match.end()))
                continue
        anchor = lower in {"at", "@"}
        if (
            lower == "at"
            and i + 2 < len(tokens)
            and tokens[i + 1].group().casefold() in {"the", "d"}
            and tokens[i + 2].group().casefold() == "rate"
        ):
            anchor = True
            i_end = i + 2
        elif lower == "at" and i + 1 < len(tokens) and tokens[i + 1].group().casefold() == "sign":
            anchor, i_end = True, i + 1
        else:
            i_end = i
        if not anchor:
            continue
        start = i_end + 1
        if start >= len(tokens):
            continue
        provider_end = None
        first = tokens[start].group().strip(",.;:!?)]}" + "'").casefold()
        if first in {"asp", "vb", "ado", "sharp", "core"}:
            continue
        split_provider = (
            start + 1 < len(tokens) and (first, tokens[start + 1].group().casefold()) in SPLIT_PROVIDERS
        )
        if first in PROVIDERS or split_provider:
            provider_end = start + 1 if split_provider else start
        end = provider_end
        if end is None:
            limit = 4
            for candidate in range(start + 1, min(len(tokens), start + limit + 1)):
                word = tokens[candidate - 1].group().strip(",.;:!?)]}" + "'").casefold()
                if word in DOMAIN_STOPS or word.isdigit():
                    break
                marker = (
                    tokens[candidate].group().strip(",.;:!?)]}" + "'").casefold()
                    if candidate < len(tokens)
                    else ""
                )
                tld_word = (
                    tokens[candidate + 1].group().strip(",.;:!?)]}" + "'")
                    if candidate + 1 < len(tokens)
                    else ""
                )
                if marker in {"period", "point", "full"} and tld_word.casefold() not in KNOWN_TLDS:
                    continue
                raw_marker = tokens[candidate].group().strip(",.;:!?)]}" + "'")
                if marker in KNOWN_TLDS:
                    if marker != "com" and raw_marker != marker:
                        continue
                    if not (
                        raw_marker.startswith(".")
                        or tokens[candidate - 1].group().rstrip().endswith(".")
                        or tokens[candidate - 1].group().casefold() in {"dot", "period", "point"}
                    ):
                        continue
                if marker == "dot" and tld_word.casefold() in FILE_ENDINGS:
                    continue
                if marker in {"dot", "period", "point", "full"} and candidate + 1 < len(tokens):
                    after = (
                        tokens[candidate + 2].group().strip(",.;:!?)]}" + "'").casefold()
                        if candidate + 2 < len(tokens)
                        else ""
                    )
                    if tld_word.casefold() != "com" and after in {
                        "developer",
                        "framework",
                        "core",
                        "technology",
                        "technologies",
                        "stack",
                        "project",
                        "projects",
                        "team",
                        "engineer",
                        "programmer",
                        "jobs",
                        "job",
                        "role",
                    }:
                        continue
                    end = candidate + 1
                    break
                if marker.startswith("dot") and marker[3:] in KNOWN_TLDS:
                    end = candidate
                    break
                if marker in KNOWN_TLDS:
                    after = (
                        tokens[candidate + 1].group().strip(",.;:!?)]}" + "'").casefold()
                        if candidate + 1 < len(tokens)
                        else ""
                    )
                    if marker != "com" and after in {
                        "developer",
                        "framework",
                        "core",
                        "technology",
                        "technologies",
                        "stack",
                        "project",
                        "projects",
                        "team",
                        "engineer",
                        "programmer",
                        "jobs",
                        "job",
                        "role",
                    }:
                        continue
                    end = candidate
                    break
        if end is not None:
            local = i > 0 and tokens[i - 1].group().strip(",.;:!?)]}" + "'").casefold() not in DOMAIN_STOPS
            if local:
                hits.append(Hit("EMAIL_LIKE", tokens[i - 1].start(), tokens[end].end()))
    return hits


def scan_text(
    text: str, allow_ids: frozenset[str] = frozenset(), precision: frozenset[str] | None = None
) -> list[Hit]:
    hits: list[Hit] = []
    money_spans = _money_spans(text)
    for name, (regex, detector_precision) in _detectors.items():
        if precision is not None and detector_precision not in precision:
            continue
        for match in regex.finditer(text):
            value = match.group()
            if value in allow_ids or _placeholder.fullmatch(value):
                continue
            if name == "DIGITS_LIKE" and any(
                match.start() < end and match.end() > start for start, end in money_spans
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
        hits.extend(_scan_spoken_email_tokens(text))
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
