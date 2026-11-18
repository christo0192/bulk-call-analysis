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
    if roster:
        text, n = roster.subn("[ADVISOR]", text)
        if n:
            counts["ROSTER"] = n

    def slot(match: re.Match[str]) -> str:
        return match.group(0) if match.group(2).casefold() in MORE_STOP else f"{match.group(1)} [NAME]"

    text, n = _cap_greet.subn(
        lambda m: m.group(0) if m.group(3).casefold() in MORE_STOP else f"{m.group(1)}{m.group(2)}[NAME]",
        text,
    )
    text, n_intro = _intro.subn(slot, text)
    n += n_intro
    text, n_slot = _cap_slot.subn(slot, text)
    n += n_slot
    text, n_call = _calling.subn(
        lambda m: m.group(0) if m.group(1).casefold() in MORE_STOP else f"[NAME]{m.group(2)}", text
    )
    n += n_call
    if n:
        counts["INTRODUCTION"] = n
    if ner:
        spans = sorted(ner.persons(text), key=lambda span: span.start, reverse=True)
        for span in spans:
            if 0 <= span.start < span.end <= len(text):
                text = text[: span.start] + "[NAME]" + text[span.end :]
                counts["NER"] = counts.get("NER", 0) + 1
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
