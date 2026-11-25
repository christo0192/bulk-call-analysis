from __future__ import annotations

import json
import random
import re
import time
from pathlib import Path

import pytest

from bca.mask.main_provider_rule import provider_replacements

_spoken_domain = re.compile(
    r"(?i)\b(?:g\s?mail|yahoo|ymail|hotmail|outlook|rediff(?:mail)?|icloud|protonmail)\b"
    r"(?:\s*(?:\.|dot)\s*(?:com|in|co|net|org)\b)?"
)


def _main_oracle(text: str) -> list[tuple[int, int]]:
    replacements: list[tuple[int, int]] = []
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
        tokens = list(re.finditer(r"\S+", text[: domain.start()]))
        if not tokens:
            continue
        j = len(tokens)
        while j and tokens[j - 1].group().strip(",.?!").casefold() in connectors:
            j -= 1
        k, meaningful = j, 0
        while k:
            token = tokens[k - 1].group().strip(",.?!")
            if len(token) > 1 and token.casefold() in stop:
                break
            if meaningful >= 6:
                break
            meaningful += len(token) > 1
            k -= 1
        if k != j:
            replacements.append((tokens[k].start(), domain.end()))
    return replacements


def _corpus_texts() -> list[str]:
    texts = [
        entry["text"]
        for entry in json.loads((Path(__file__).parent / "email_vs_main_v3.json").read_text(encoding="utf-8"))
    ]
    corpus = json.loads((Path(__file__).parent / "email_corpus.json").read_text(encoding="utf-8"))
    texts.extend(
        entry["text"] if isinstance(entry, dict) else entry
        for entries in corpus.values()
        for entry in entries
    )
    return texts


def _random_texts() -> list[str]:
    vocabulary = [
        "ravi",
        "kumar",
        "priya",
        "92",
        "1990",
        "a",
        "b",
        "c",
        "x",
        "at",
        "the",
        "rate",
        "of",
        "@",
        "dot",
        "com",
        "in",
        "co",
        "gmail",
        "yahoo",
        "ymail",
        "hotmail",
        "outlook",
        "rediff",
        "rediffmail",
        "icloud",
        "protonmail",
        "g",
        "mail",
        "is",
        "id",
        "email",
        "my",
        "send",
        "to",
        "please",
        "okay,",
        "sir.",
        "Lead:",
        "Advisor:",
        "and",
    ]
    rng = random.Random(7)
    return [" ".join(rng.choices(vocabulary, k=rng.randint(1, 30))) for _ in range(3000)]


def test_main_provider_rule_matches_main() -> None:
    for text in _corpus_texts() + _random_texts():
        assert sorted(set(provider_replacements(text))) == sorted(set(_main_oracle(text)))


@pytest.mark.parametrize("unit", ["ravi at gmail dot com ", "gmail-", "x.gmail"])
def test_main_provider_rule_is_linear(unit: str) -> None:
    # The last two units build ONE long token holding thousands of provider words.
    def best_of_three(size: int) -> float:
        text = (unit * (size // len(unit) + 1))[:size]
        provider_replacements(text[:1000])
        timings = []
        for _ in range(3):
            started = time.perf_counter()
            provider_replacements(text)
            timings.append(time.perf_counter() - started)
        return min(timings)

    small = best_of_three(50_000)
    large = best_of_three(200_000)
    # Quadratic growth is 16x for 4x the input; 8x allows timer noise when the small run takes a
    # few milliseconds (a fast CI machine measured 6.3x on linear code), as the other growth tests do.
    assert large / max(small, 0.02) < 8


def _rough_texts() -> list[str]:
    """Long tokens, punctuation runs and long runs of one-character words, which the plain
    vocabulary above never builds: gate 10 found the port differed from main after 64 or more
    one-character tokens, and re-scanned a long trailing punctuation run for every match."""
    rng = random.Random(10)
    words = [
        "ravi",
        "kumar",
        "a",
        "s",
        "at",
        "the",
        "rate",
        "of",
        "@",
        "dot",
        "com",
        "gmail",
        "yahoo",
        "g",
        "mail",
        "is",
        "my",
        "okay,",
        "sir.",
        "-",
        ",",
        "...",
        "x.gmail",
        "a'gmail",
        "gmail-",
        "@gmail",
        "AT",
        "Rate",
        "it's",
        "ß",
        "ﬁ",
        "!!at!!",
        "gmail.com,",
    ]

    def long_token() -> str:
        size = rng.randint(30, 300)
        return rng.choice(
            [
                "x" + "." * size,
                "!" * size,
                "gmail-" * (size // 6 + 1),
                "a" * size,
                "".join(rng.choice("ab.,!?-'@") for _ in range(size)),
            ]
        )

    texts = []
    for _ in range(3000):
        parts = []
        for _ in range(rng.randint(1, 30)):
            roll = rng.random()
            if roll < 0.06:
                parts.append(long_token())
            elif roll < 0.10:
                parts.append(" ".join(rng.choice("abcsu-,.@") for _ in range(rng.randint(40, 120))))
            else:
                parts.append(rng.choice(words))
        texts.append(rng.choice([" ", "  ", "\n"]).join(parts))
    return texts


@pytest.mark.parametrize(
    "text",
    [
        "s u r e s h k u m a r " * 6 + "at gmail dot com",
        "ok " + "- " * 70 + "ravi at gmail dot com",
        "ok " + ", " * 70 + "gmail.com",
        "at " * 300 + "gmail",
        "@ " * 100 + "@gmail @gmail @gmail",
    ],
)
def test_main_provider_rule_matches_main_after_long_runs(text: str) -> None:
    # Main walks back with no token limit; the port must too.
    assert sorted(set(provider_replacements(text))) == sorted(set(_main_oracle(text)))


def test_main_provider_rule_matches_main_on_rough_texts() -> None:
    for text in _rough_texts():
        assert sorted(set(provider_replacements(text))) == sorted(set(_main_oracle(text)))


@pytest.mark.parametrize(
    "build",
    [
        # A long trailing punctuation run, then one token full of provider words.
        lambda size: "x" + "." * (size // 2) + " " + ("gmail-" * (size // 12 + 1))[: size // 2],
        lambda size: "word" + "!" * (size // 2) + " " + ("yahoo." * (size // 12 + 1))[: size // 2],
        # Thousands of one-character words before thousands of matches.
        lambda size: "a " * (size // 4) + ("gmail-" * (size // 12 + 1))[: size // 2],
        lambda size: "at " * (size // 6) + ("gmail " * (size // 12 + 1))[: size // 2],
    ],
    ids=["dots-then-gmail", "bangs-then-yahoo", "one-char-words", "connectors"],
)
def test_main_provider_rule_is_linear_on_shaped_texts(build: object) -> None:
    def best_of_three(size: int) -> float:
        text = build(size)  # type: ignore[operator]
        timings = []
        for _ in range(3):
            started = time.perf_counter()
            provider_replacements(text)
            timings.append(time.perf_counter() - started)
        return min(timings)

    small = best_of_three(50_000)
    large = best_of_three(200_000)
    # Quadratic growth is 16x for 4x the input; 8x allows timer noise when the small run takes a
    # few milliseconds (a fast CI machine measured 6.3x on linear code), as the other growth tests do.
    assert large / max(small, 0.02) < 8
