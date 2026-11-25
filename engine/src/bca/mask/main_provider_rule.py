"""Linear port of the main application's spoken webmail rule.

Main's rule (`_spoken_email_replacements` on main) re-tokenises `text[: match.start()]` for every
provider match and strips and casefolds each token it walks over. The results here are the same,
which `tests/mask/test_main_provider_rule.py` checks against main's code kept verbatim, but every
token is tokenised once, and a token is never copied or casefolded when it is longer than any word
the rule compares it with. So one long unbroken token full of provider words stays linear.
"""

from __future__ import annotations

import re
from bisect import bisect_left

_spoken_domain = re.compile(
    r"(?i)\b(?:g\s?mail|yahoo|ymail|hotmail|outlook|rediff(?:mail)?|icloud|protonmail)\b"
    r"(?:\s*(?:\.|dot)\s*(?:com|in|co|net|org)\b)?"
)
_TOKEN = re.compile(r"\S+")
_PUNCTUATION = frozenset(",.?!")
_STOP = frozenset(
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
    }
)
_CONNECTORS = frozenset({"at", "the", "rate", "of", "@"})
# Longer than every word in _STOP and _CONNECTORS: a stripped piece this long can't match one.
_LONGEST = max(len(word) for word in _STOP | _CONNECTORS)
# Stands for any stripped piece longer than _LONGEST: never a stop word or connector, length > 1.
_LONG = "\x00long piece"


def provider_replacements(text: str) -> list[tuple[int, int]]:
    tokens = [(match.start(), match.end()) for match in _TOKEN.finditer(text)]
    starts = [start for start, _ in tokens]
    # Where each token's leading ",.?!" run ends; computed once, so a long run is scanned once.
    left_strip: list[int] = []
    for start, end in tokens:
        position = start
        while position < end and text[position] in _PUNCTUATION:
            position += 1
        left_strip.append(position)

    def stripped(index: int, end: int) -> tuple[str, int]:
        """`text[token_start:end].strip(",.?!")` casefolded, and its length (a short form only)."""
        left = min(left_strip[index], end)
        right = end
        while right > left and text[right - 1] in _PUNCTUATION:
            right -= 1
        length = right - left
        if length > _LONGEST:
            return _LONG, length
        return text[left:right].casefold(), length

    replacements: list[tuple[int, int]] = []
    for domain in _spoken_domain.finditer(text):
        cut = domain.start()
        # Main tokenises text[:cut]: the tokens that start before the match, the last one cut at it.
        j = bisect_left(starts, cut)
        if not j:
            continue
        last = j - 1  # the token main sees cut at the match

        def piece(index: int, cut: int = cut, last: int = last) -> tuple[str, int]:
            end = tokens[index][1]
            return stripped(index, min(end, cut) if index == last else end)

        while j and piece(j - 1)[0] in _CONNECTORS:
            j -= 1
        k, meaningful = j, 0
        lower_bound = max(0, j - 64)
        while k > lower_bound:
            value, length = piece(k - 1)
            if length > 1 and value in _STOP:
                break
            if meaningful >= 6:
                break
            meaningful += length > 1
            k -= 1
        if k != j:
            replacements.append((tokens[k][0], domain.end()))
    return replacements
