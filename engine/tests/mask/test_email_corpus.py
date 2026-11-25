from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from bca.mask.leakscan import scan_text
from bca.mask.masker import MaskingContext, mask_text

CORPUS = json.loads((Path(__file__).parent / "email_corpus.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CORPUS["must_mask"])
def test_corpus_must_mask(case: dict[str, object]) -> None:
    text = str(case["text"])
    output = mask_text(text, MaskingContext()).text
    gone = cast(list[str], case["gone"])
    keeps = cast(list[str], case["keeps"])
    assert "[EMAIL]" in output
    assert all(value.casefold() not in output.casefold() for value in gone)
    assert all(value in output for value in keeps)
    assert not any(hit.detector == "EMAIL_LIKE" for hit in scan_text(output))


@pytest.mark.parametrize("text", CORPUS["must_not_change"])
def test_corpus_must_not_change(text: str) -> None:
    assert mask_text(text, MaskingContext()).text == text
    assert not any(hit.detector == "EMAIL_LIKE" for hit in scan_text(text))


@pytest.mark.parametrize("text", CORPUS["net_must_hold_unmasked"])
def test_corpus_net_holds_unmasked(text: str) -> None:
    assert any(hit.detector == "EMAIL_LIKE" for hit in scan_text(text))
