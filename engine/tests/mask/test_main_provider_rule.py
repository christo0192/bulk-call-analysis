from __future__ import annotations

import json
import random
import re
import time
from pathlib import Path

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


def test_main_provider_rule_is_linear() -> None:
    def best_of_three(size: int) -> float:
        text = ("ravi at gmail dot com " * (size // 20 + 1))[:size]
        provider_replacements(text[:1000])
        timings = []
        for _ in range(3):
            started = time.perf_counter()
            provider_replacements(text)
            timings.append(time.perf_counter() - started)
        return min(timings)

    small = best_of_three(50_000)
    large = best_of_three(200_000)
    assert large / small < 6
