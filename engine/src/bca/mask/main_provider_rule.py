"""Linear port of the main application's spoken webmail rule."""

from __future__ import annotations

import re
from bisect import bisect_left

_spoken_domain = re.compile(
    r"(?i)\b(?:g\s?mail|yahoo|ymail|hotmail|outlook|rediff(?:mail)?|icloud|protonmail)\b"
    r"(?:\s*(?:\.|dot)\s*(?:com|in|co|net|org)\b)?"
)
_TOKEN = re.compile(r"\S+")


def provider_replacements(text: str) -> list[tuple[int, int]]:
    replacements: list[tuple[int, int]] = []
    token_matches = list(_TOKEN.finditer(text))
    token_starts = [match.start() for match in token_matches]
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
        j = bisect_left(token_starts, domain.start())
        if not j:
            continue
        while j and token_matches[j - 1].group().strip(",.?!").casefold() in connectors:
            j -= 1
        k, meaningful = j, 0
        lower_bound = max(0, j - 64)
        while k > lower_bound:
            token = token_matches[k - 1].group().strip(",.?!")
            if len(token) > 1 and token.casefold() in stop:
                break
            if meaningful >= 6:
                break
            meaningful += len(token) > 1
            k -= 1
        if k != j:
            replacements.append((token_matches[k].start(), domain.end()))
    return replacements
