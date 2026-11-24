from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import pytest

from bca.mask.leakscan import scan_text
from bca.mask.masker import MaskingContext, mask_text

ENTRIES = json.loads((Path(__file__).parent / "email_vs_main.json").read_text(encoding="utf-8"))
WORD = re.compile(r"[a-z0-9]+")
CONNECTORS = frozenset(
    [
        "dot",
        "com",
        "in",
        "co",
        "org",
        "net",
        "edu",
        "gov",
        "io",
        "info",
        "ai",
        "me",
        "at",
        "the",
        "rate",
        "of",
        "sign",
        "period",
        "point",
        "full",
        "stop",
        "underscore",
        "hyphen",
        "dash",
        "d",
        "red",
    ]
)
ORDINARY_WORDS = frozenset(
    [
        "my",
        "email",
        "mail",
        "id",
        "is",
        "it",
        "its",
        "to",
        "send",
        "sent",
        "note",
        "write",
        "down",
        "please",
        "contact",
        "details",
        "number",
        "upi",
        "and",
        "or",
        "you",
        "your",
        "sir",
        "madam",
        "yes",
        "ok",
        "okay",
        "so",
        "the",
        "a",
        "an",
        "this",
        "that",
        "on",
        "for",
        "me",
        "i",
        "am",
    ]
)


def words(text: str) -> Counter[str]:
    cleaned = text.replace("[EMAIL]", " ").replace("[PHONE]", " ").replace("[UPI]", " ")
    return Counter(
        word
        for word in WORD.findall(cleaned.casefold())
        if word not in CONNECTORS and word not in ORDINARY_WORDS
    )


@pytest.mark.parametrize("entry", ENTRIES)
def test_never_worse_than_main_on_addresses(entry: dict[str, str]) -> None:
    if entry["kind"] != "address":
        pytest.skip("ordinary entry")
    result = mask_text(entry["text"], MaskingContext()).text
    removed = words(entry["text"]) - words(entry["main"])
    assert not (removed & words(result))


@pytest.mark.parametrize("entry", ENTRIES)
def test_ordinary_sentences_unchanged_and_not_held(entry: dict[str, str]) -> None:
    if entry["kind"] != "ordinary":
        pytest.skip("address entry")
    result = mask_text(entry["text"], MaskingContext()).text
    assert result == entry["main"] == entry["text"]
    assert not any(hit.precision == "high" and hit.detector == "EMAIL_LIKE" for hit in scan_text(result))
