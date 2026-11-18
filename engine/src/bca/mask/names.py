"""Roster, introduction, and pluggable PERSON masking."""

from __future__ import annotations

import importlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class Span:
    start: int
    end: int


class NerBackend(Protocol):
    def persons(self, text: str) -> list[Span]: ...


class NerUnavailable(RuntimeError):
    """spaCy or its English model is not available."""


class HarvestMissing(RuntimeError):
    """A required per-job harvested-name list is absent."""


class SpacyNer:
    def __init__(self) -> None:
        try:
            spacy = importlib.import_module("spacy")
            self._nlp = spacy.load(
                "en_core_web_sm", disable=["parser", "lemmatizer", "tagger", "attribute_ruler"]
            )
        except (ImportError, OSError) as exc:
            raise NerUnavailable("spaCy and the en_core_web_sm model are required for SpacyNer") from exc

    def persons(self, text: str) -> list[Span]:
        return [Span(ent.start_char, ent.end_char) for ent in self._nlp(text).ents if ent.label_ == "PERSON"]


def roster_pattern(path: Path | None) -> re.Pattern[str] | None:
    if path is None:
        return None
    names = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    names += [part for name in names for part in name.split() if len(part) >= 3]
    if not names:
        return None
    return re.compile(
        r"(?<!\w)(?:"
        + "|".join(re.escape(name) for name in sorted(set(names), key=len, reverse=True))
        + r")(?!\w)",
        re.I,
    )


_intro = re.compile(
    r"(?i)\b(this is|my name is)\s+([A-Za-z]+(?:\s+(?!from\b)[A-Za-z]+){0,2})(?=\s*(?:from\b|[,.!?]|$))"
)


def mask_names(
    text: str, roster: re.Pattern[str] | None, ner: NerBackend | None, harvested: Iterable[str]
) -> tuple[str, dict[str, int]]:
    counts: dict[str, int] = {}
    if roster:
        text, n = roster.subn("[ADVISOR]", text)
        if n:
            counts["ROSTER"] = n
    text, n = _intro.subn(lambda m: f"{m.group(1)} [NAME]", text)
    if n:
        counts["INTRODUCTION"] = n
    if ner:
        spans = sorted(ner.persons(text), key=lambda span: span.start, reverse=True)
        for span in spans:
            if 0 <= span.start < span.end <= len(text):
                text = text[: span.start] + "[NAME]" + text[span.end :]
                counts["NER"] = counts.get("NER", 0) + 1
    harvested_pattern = [name for name in harvested if name.strip()]
    if harvested_pattern:
        pattern = re.compile(
            r"(?<!\w)(?:"
            + "|".join(re.escape(n) for n in sorted(harvested_pattern, key=len, reverse=True))
            + r")(?!\w)",
            re.I,
        )
        text, n = pattern.subn("[NAME]", text)
        if n:
            counts["HARVESTED"] = n
    return text, counts


def harvest_seen_twice(calls: Iterable[str], ner: NerBackend) -> list[str]:
    seen: dict[str, int] = {}
    canonical: dict[str, str] = {}
    for text in calls:
        for span in ner.persons(text):
            name = text[span.start : span.end].strip()
            key = name.casefold()
            seen[key] = seen.get(key, 0) + 1
            canonical[key] = name
    return sorted((canonical[key] for key, count in seen.items() if count >= 2), key=str.casefold)
