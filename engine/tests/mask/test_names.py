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


class RosterNer:
    def persons(self, text: str) -> list[Span]:
        name = "Ravi Kumar"
        start = text.find(name)
        return [Span(start, start + len(name))] if start >= 0 else []


class FullNameNer:
    def __init__(self, name: str) -> None:
        self.name = name

    def persons(self, text: str) -> list[Span]:
        start = text.find(self.name)
        return [Span(start, start + len(self.name))] if start >= 0 else []


def test_roster_full_and_first_names_have_boundaries(tmp_path: Path) -> None:
    roster = tmp_path / "roster.txt"
    roster.write_text("Ravi Kumar\n", encoding="utf-8")
    text = mask_text("Ravi met Ravi Kumar; bravery stayed intact.", MaskingContext(roster_file=roster)).text
    assert text == "[ADVISOR] met [ADVISOR]; bravery stayed intact."


def test_introductions() -> None:
    result = mask_text("This is Ravi from support. My name is Neha Rao.", MaskingContext()).text
    assert result == "This is [NAME] from support. My name is [NAME]."


def test_roster_name_stays_advisor_with_ner_present(tmp_path: Path) -> None:
    roster = tmp_path / "roster.txt"
    roster.write_text("Ravi Kumar\n", encoding="utf-8")
    result = mask_text(
        "This is Ravi Kumar from support.",
        MaskingContext(roster_file=roster, ner=RosterNer()),
    )
    assert result.text == "This is [ADVISOR] from support."


def test_lead_sharing_advisor_first_name_is_fully_masked(tmp_path: Path) -> None:
    roster = tmp_path / "roster.txt"
    roster.write_text("Ravi\n", encoding="utf-8")
    result = mask_text(
        "Ravi Sharma will join.", MaskingContext(roster_file=roster, ner=FullNameNer("Ravi Sharma"))
    )
    assert result.text == "[NAME] will join."


def test_lead_sharing_advisor_surname_is_fully_masked(tmp_path: Path) -> None:
    roster = tmp_path / "roster.txt"
    roster.write_text("Kumar\n", encoding="utf-8")
    result = mask_text(
        "Anil Kumar will join.", MaskingContext(roster_file=roster, ner=FullNameNer("Anil Kumar"))
    )
    assert result.text == "[NAME] will join."


def test_full_advisor_name_stays_advisor_with_ner(tmp_path: Path) -> None:
    roster = tmp_path / "roster.txt"
    roster.write_text("Ravi Kumar\n", encoding="utf-8")
    result = mask_text(
        "Ravi Kumar will join.", MaskingContext(roster_file=roster, ner=FullNameNer("Ravi Kumar"))
    )
    assert result.text == "[ADVISOR] will join."


def test_no_name_token_survives_overlap_resolution(tmp_path: Path) -> None:
    pairs = (
        ("Ravi", "Ravi Sharma"),
        ("Kumar", "Anil Kumar"),
        ("Neha", "Neha Rao"),
        ("Shah", "Kabir Shah"),
        ("Asha", "Asha Patel"),
        ("Singh", "Mira Singh"),
        ("Dev", "Dev Mehta"),
        ("Iyer", "Rohan Iyer"),
        ("Maya", "Maya Das"),
        ("Roy", "Anita Roy"),
    )
    for advisor_part, lead_name in pairs:
        roster = tmp_path / f"{advisor_part}.txt"
        roster.write_text(advisor_part + "\n", encoding="utf-8")
        result = mask_text(lead_name, MaskingContext(roster_file=roster, ner=FullNameNer(lead_name))).text
        assert all(token.casefold() not in result.casefold() for token in lead_name.split())


def test_roster_and_lead_names_in_one_sentence(tmp_path: Path) -> None:
    roster = tmp_path / "roster.txt"
    roster.write_text("Ravi Kumar\n", encoding="utf-8")
    result = mask_text(
        "Ravi Kumar spoke with Neha Rao.",
        MaskingContext(roster_file=roster, harvested_names=("Neha Rao",)),
    )
    assert result.text == "[ADVISOR] spoke with [NAME]."


def test_name_spans_across_a_line_break_stay_separate() -> None:
    class BreakNer:
        def persons(self, text: str) -> list[Span]:
            return [Span(3, 7), Span(8, 11)]

    result = mask_text("Hi Neha\nRao", MaskingContext(ner=BreakNer()))
    assert result.text == "Hi [NAME]\n[NAME]"


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
