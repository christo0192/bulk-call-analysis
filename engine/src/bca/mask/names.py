"""Roster, introduction, and pluggable PERSON masking."""

from __future__ import annotations

import importlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
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


@lru_cache(maxsize=32)
def roster_pattern(path: Path | None) -> re.Pattern[str] | None:
    if path is None:
        return None
    names = [line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    keep = {"more", "deep", "dev", "may", "will", "fine", "team", "course", "program", "the", "and"}
    names += [
        part for name in names for part in name.split() if len(part) >= 3 and part.casefold() not in keep
    ]
    if not names:
        return None
    return re.compile(
        r"(?<!\w)(?:"
        + "|".join(re.escape(name) for name in sorted(set(names), key=len, reverse=True))
        + r")(?!\w)",
        re.I,
    )


MORE_STOP = {
    "so",
    "ok",
    "okay",
    "and",
    "but",
    "yes",
    "no",
    "dear",
    "mr",
    "mrs",
    "ms",
    "miss",
    "dr",
    "see",
    "like",
    "bye",
    "also",
    "actually",
    "basically",
    "because",
    "then",
    "where",
    "when",
    "why",
    "who",
    "great",
    "nice",
    "perfect",
    "fine",
    "cool",
    "interview",
    "program",
    "programme",
    "course",
    "team",
    "manager",
    "sir",
    "madam",
    "mam",
    "ji",
    "hmm",
    "uh",
    "um",
    "oh",
    "ah",
    "is",
    "are",
    "was",
}
_intro = re.compile(
    r"\b((?i:this is|my name is))\s+([A-Z][a-zA-Z]{2,}(?:\s+[A-Z][a-zA-Z]{2,})?)"
    r"(?=\s*(?:from\b|and\b|[,.!?]|$))"
)
_cap_greet = re.compile(r"\b(Hi|Hello|Hey|Dear)([,!]?[\s]+)([A-Z][a-zA-Z]{2,})\b")
_cap_slot = re.compile(
    r"\b(This is|My name is|Myself|I am|I'm)\s+([A-Z][a-zA-Z]{2,})"
    r"(?:\s+([A-Z][a-zA-Z]{2,}))?(?=\s*(?:[,.!?]|from\b|here\b|speaking\b|calling\b|this side\b|$))"
)
_calling = re.compile(r"\b([A-Z][a-zA-Z]{2,})(\s+)(?=(?:calling|this side|here)\s+(?:from|with|on behalf)\b)")


def mask_names(
    text: str, roster: re.Pattern[str] | None, ner: NerBackend | None, harvested: Iterable[str]
) -> tuple[str, dict[str, int]]:
    counts: dict[str, int] = {}
    original = text
    roster_spans = [(match.start(), match.end()) for match in roster.finditer(original)] if roster else []
    ner_spans = ner.persons(original) if ner else []
    regex_spans: list[tuple[int, int]] = []
    regex_count = 0

    def collect(pattern: re.Pattern[str], groups: tuple[int, ...]) -> None:
        nonlocal regex_count
        for match in pattern.finditer(original):
            if any(
                match.group(group) is not None and match.group(group).casefold() not in MORE_STOP
                for group in groups
            ):
                regex_count += 1
                regex_spans.extend(
                    match.span(group)
                    for group in groups
                    if match.group(group) is not None and match.group(group).casefold() not in MORE_STOP
                )

    collect(_cap_greet, (3,))
    collect(_intro, (2,))
    collect(_cap_slot, (2, 3))
    collect(_calling, (1,))
    candidate_spans = regex_spans + [(span.start, span.end) for span in ner_spans]
    spans: list[tuple[int, int, bool]] = [
        (start, end, False)
        for start, end in candidate_spans
        if not any(start < roster_end and end > roster_start for roster_start, roster_end in roster_spans)
    ] + [(start, end, True) for start, end in roster_spans]
    merged: list[tuple[int, int, bool]] = []
    for start, end, is_roster in sorted(spans):
        if start < 0 or end <= start or end > len(original):
            continue
        if merged and (
            start <= merged[-1][1] or all(char in " \t" for char in original[merged[-1][1] : start])
        ):
            merged[-1] = (merged[-1][0], max(merged[-1][1], end), merged[-1][2] or is_roster)
        else:
            merged.append((start, end, is_roster))
    for start, end, is_roster in reversed(merged):
        text = text[:start] + ("[ADVISOR]" if is_roster else "[NAME]") + text[end:]
    if regex_count:
        counts["INTRODUCTION"] = regex_count
    if ner_spans:
        counts["NER"] = sum(0 <= span.start < span.end <= len(original) for span in ner_spans)
    if roster_spans:
        counts["ROSTER"] = len(roster_spans)
    pattern = harvested_pattern(tuple(name for name in harvested if name.strip()))
    if pattern:
        text, n = pattern.subn("[NAME]", text)
        if n:
            counts["HARVESTED"] = n
    return text, counts


@lru_cache(maxsize=32)
def harvested_pattern(names: tuple[str, ...]) -> re.Pattern[str] | None:
    if not names:
        return None
    return re.compile(
        r"(?<!\w)(?:" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True)) + r")(?!\w)",
        re.I,
    )


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
