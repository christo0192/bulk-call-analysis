"""Independent, broader post-mask leak scanning."""

from __future__ import annotations

import gzip
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


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
    "PHONE_LIKE": (re.compile(r"(?<!\w)(?:\+?\d[\d ()-]{7,}\d)(?!\w)"), "high"),
    "EMAIL_LIKE": (
        re.compile(r"(?i)(?<!\w)[\w.+-]+\s*(?:@|at)\s*[\w.-]+(?:\s*dot\s*|\.)[a-z]{2,}(?!\w)"),
        "high",
    ),
    "PAN_LIKE": (re.compile(r"(?i)\b[a-z]{5}[ -]?\d{4}[ -]?[a-z]\b"), "high"),
    "AADHAAR_LIKE": (re.compile(r"(?<!\d)(?:\d{4}[ -]?){2}\d{4}(?!\d)"), "high"),
    "NUMBER_WORDS": (
        re.compile(
            r"(?i)\b(?:zero|oh|one|two|three|four|five|six|seven|eight|nine)(?:\s+(?:zero|oh|one|two|three|four|five|six|seven|eight|nine)){5,}\b"
        ),
        "high",
    ),
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
            hits.append(Hit(name, match.start(), match.end(), detector_precision))
    return sorted(hits, key=lambda hit: (hit.start, hit.end, hit.detector))


def scan_lead(
    masked_file: Path, allow_ids: frozenset[str] = frozenset(), precision: frozenset[str] | None = None
) -> LeakReport:
    hits: list[Hit] = []
    with gzip.open(masked_file, "rt", encoding="utf-8") as source:
        for line in source:
            record = json.loads(line)
            text = record.get("transcript_text", record.get("text", ""))
            if isinstance(text, str):
                hits.extend(scan_text(text, allow_ids, precision))
    return LeakReport(hits)


def calibrate(files: Iterable[Path], allow_ids: frozenset[str] = frozenset()) -> dict[str, int]:
    totals: dict[str, int] = {}
    for path in files:
        report = scan_lead(path, allow_ids)
        for hit in report.hits:
            totals[hit.detector] = totals.get(hit.detector, 0) + 1
    return totals
