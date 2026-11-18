from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from bca.mask.masker import MaskingContext, mask_text
from bca.mask.names import HarvestMissing, NerUnavailable, SpacyNer, Span, harvest_seen_twice


class FakeNer:
    def persons(self, text: str) -> list[Span]:
        result: list[Span] = []
        for name in ("Neha Rao", "Kabir Shah"):
            start = text.find(name)
            if start >= 0:
                result.append(Span(start, start + len(name)))
        return result


def test_roster_full_and_first_names_have_boundaries(tmp_path: Path) -> None:
    roster = tmp_path / "roster.txt"
    roster.write_text("Ravi Kumar\n", encoding="utf-8")
    text = mask_text("Ravi met Ravi Kumar; bravery stayed intact.", MaskingContext(roster_file=roster)).text
    assert text == "[ADVISOR] met [ADVISOR]; bravery stayed intact."


def test_introductions() -> None:
    result = mask_text("This is Ravi from support. My name is Neha Rao.", MaskingContext()).text
    assert result == "This is [NAME] from support. My name is [NAME]."


def test_fake_ner_and_harvested_names() -> None:
    fake = FakeNer()
    assert mask_text("Neha Rao spoke.", MaskingContext(ner=fake)).text == "[NAME] spoke."
    assert harvest_seen_twice(["Neha Rao called", "Neha Rao followed up", "Kabir Shah called"], fake) == [
        "Neha Rao"
    ]
    assert (
        mask_text("Neha Rao and Kabir Shah", MaskingContext(harvested_names=("Neha Rao",))).text
        == "[NAME] and Kabir Shah"
    )


def test_missing_harvest_fails_closed() -> None:
    with pytest.raises(HarvestMissing):
        mask_text("ordinary text", MaskingContext(require_harvest=True, harvested_names=None))


def test_spacy_unavailable_is_explicit() -> None:
    if importlib.util.find_spec("spacy") is None:
        with pytest.raises(NerUnavailable):
            SpacyNer()
