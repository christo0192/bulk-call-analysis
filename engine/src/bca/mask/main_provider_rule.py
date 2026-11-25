"""Linear port of the main application's spoken webmail rule.

Main's rule (`_spoken_email_replacements` on main) re-tokenises `text[: match.start()]` for every
provider match, then walks back over the tokens: past any connector words ("at", "the rate of",
"@"), then over at most six words of two or more characters, stopping before a stop word. The
spans here are the same, which `tests/mask/test_main_provider_rule.py` checks against main's code
kept verbatim. But each token is tokenised, stripped and classified once, and each walk is a few
look-ups (the nearest stop word, the start of a connector run, the sixth word back), so the time
stays linear in the text however the tokens and matches are arranged.
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
    # Each token's ",.?!"-stripped bounds, found once: a long punctuation run is scanned once.
    left_strip: list[int] = []
    right_strip: list[int] = []
    for start, end in tokens:
        left = start
        while left < end and text[left] in _PUNCTUATION:
            left += 1
        right = end
        while right > left and text[right - 1] in _PUNCTUATION:
            right -= 1
        left_strip.append(left)
        right_strip.append(right)

    def classify(left: int, right: int) -> tuple[bool, bool, bool]:
        """(connector, stop word, two or more characters) for the stripped piece text[left:right]."""
        length = right - left
        value = text[left:right].casefold() if length <= _LONGEST else _LONG
        return value in _CONNECTORS, length > 1 and value in _STOP, length > 1

    connector: list[bool] = []
    nearest_stop: list[int] = []  # the last stop-word token at or before each index, or -1
    run_start: list[int] = []  # where the run of connector tokens ending at each index starts
    words: list[int] = []  # indexes of tokens of two or more characters, in order
    last_stop = -1
    for index in range(len(tokens)):
        is_connector, is_stop, is_word = classify(left_strip[index], right_strip[index])
        connector.append(is_connector)
        if is_stop:
            last_stop = index
        nearest_stop.append(last_stop)
        run_start.append(run_start[index - 1] if index and connector[index - 1] else index)
        if is_word:
            words.append(index)

    def walk_end(below: int, counted: int) -> int:
        """Where main's walk over whole tokens [0, below) stops, with `counted` words already taken.

        It stops just after the nearest stop word, or at the word that makes six, or at 0.
        """
        if not below:
            return 0
        bound = nearest_stop[below - 1] + 1
        sixth = bisect_left(words, below) - (6 - counted)
        if sixth >= 0:
            bound = max(bound, words[sixth])
        return bound

    replacements: list[tuple[int, int]] = []
    for domain in _spoken_domain.finditer(text):
        cut = domain.start()
        # Main tokenises text[:cut]: the tokens that start before the match, the last one cut at it.
        j = bisect_left(starts, cut)
        if not j:
            continue
        last = j - 1
        if tokens[last][1] > cut:
            # The match starts inside this token, so main sees only the part before the match.
            left = min(left_strip[last], cut)
            right = cut
            while right > left and text[right - 1] in _PUNCTUATION:
                right -= 1
            is_connector, is_stop, is_word = classify(left, right)
            if is_connector:
                j = run_start[last - 1] if last and connector[last - 1] else last
                k = walk_end(j, 0)
            elif is_stop:
                k = j
            else:
                k = walk_end(last, int(is_word))
        else:
            if connector[last]:
                j = run_start[last]
            k = walk_end(j, 0)
        if k != j:
            replacements.append((tokens[k][0], domain.end()))
    return replacements
