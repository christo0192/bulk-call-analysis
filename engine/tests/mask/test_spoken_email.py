from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from bca.mask.leakscan import scan_lead, scan_text
from bca.mask.masker import MaskingContext, mask_text

NET_ALONE = json.loads((Path(__file__).parent / "email_net_alone.json").read_text(encoding="utf-8"))


def test_spoken_work_email_is_masked() -> None:
    expected = {
        "ravi dot kumar at acme dot co dot in": "[EMAIL]",
        "someone at example dot com": "[EMAIL]",
        "contact ravi_kumar at example dot org": "contact [EMAIL]",
    }
    for text, result in expected.items():
        assert mask_text(text, MaskingContext()).text == result


def test_spoken_email_false_positives_are_left_alone() -> None:
    for text in ("meet at the office", "at 5 dot 30", "look at this dot"):
        assert mask_text(text, MaskingContext()).text == text


def test_overlapping_email_rules_do_not_delete_following_text() -> None:
    text = "my email is ravi dot kumar at gmail dot co dot in and I am from Pune"
    assert mask_text(text, MaskingContext()).text == "my email is [EMAIL] and I am from Pune"


def test_email_then_speaker_label_is_preserved() -> None:
    assert mask_text("ravi at gmail dot co dot in. Advisor: thanks", MaskingContext()).text == (
        "[EMAIL]. Advisor: thanks"
    )


def test_span_merge_keeps_text_between_separate_spans() -> None:
    assert mask_text("Call 9876543210 and email ravi at acme dot info", MaskingContext()).text == (
        "Call [PHONE] and email [EMAIL]"
    )


def test_spoken_email_at_the_rate_is_masked() -> None:
    assert mask_text("my email is ravi kumar at the rate acme dot info", MaskingContext()).text == (
        "my email is [EMAIL]"
    )


def test_spoken_email_followed_by_punctuation_is_masked() -> None:
    for punctuation in (")", ";", ":", '"', "'s"):
        text = f"ravi at acme dot com{punctuation}"
        assert mask_text(text, MaskingContext()).text == f"[EMAIL]{punctuation}"


def test_spoken_email_with_any_tld_and_hyphenated_domain_is_masked() -> None:
    for text in (
        "ravi at acme dot info",
        "ravi at acme dot me",
        "ravi at my-company dot co",
    ):
        assert mask_text(text, MaskingContext()).text == "[EMAIL]"


def test_spoken_email_local_part_fully_masked() -> None:
    for text in (
        "ravi one two three at acme dot com",
        "r dot kumar underscore two at acme dot com",
    ):
        assert mask_text(text, MaskingContext()).text == "[EMAIL]"


def test_spoken_email_known_false_positives_still_left_alone() -> None:
    for text in ("meet at the office", "at 5 dot 30", "look at this dot"):
        assert mask_text(text, MaskingContext()).text == text


def test_accepted_over_masking_cases_are_documented() -> None:
    for text in ("visit us at example dot com", "I work at amazon dot in"):
        assert mask_text(text, MaskingContext()).text == text
        assert not any(hit.detector == "EMAIL_LIKE" for hit in scan_text(text))


def test_leak_scan_holds_back_an_unmasked_spoken_email(tmp_path: Path) -> None:
    assert any(hit.detector == "EMAIL_LIKE" for hit in scan_text("someone at example dot com"))
    path = tmp_path / "lead.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps({"transcript_text": "someone at example dot com"}) + "\n")
    assert scan_lead(path).held_back


def test_leak_scan_email_net_is_independent_of_the_masker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("bca.mask.spoken_email.find_spoken_emails", lambda text: [])
    monkeypatch.setattr("bca.mask.patterns._spoken_email_replacements", lambda text: [])
    assert any(hit.detector == "EMAIL_LIKE" for hit in scan_text("contact ravi at the rate acme dot info"))


@pytest.mark.parametrize("text", NET_ALONE)
def test_net_holds_common_forms_when_the_masker_is_broken(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    monkeypatch.setattr("bca.mask.spoken_email.find_spoken_emails", lambda value: [])
    monkeypatch.setattr("bca.mask.patterns._spoken_email_replacements", lambda value: [])
    assert any(hit.detector == "EMAIL_LIKE" for hit in scan_text(mask_text(text, MaskingContext()).text))


def test_net_module_imports_no_parser_function() -> None:
    import ast
    from pathlib import Path

    tree = ast.parse(Path(__file__).parents[2].joinpath("src/bca/mask/leakscan.py").read_text())
    imports = [
        node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module == "spoken_email"
    ]
    assert imports
    assert all(alias.name.isupper() for node in imports for alias in node.names)


def test_leak_scan_email_net_ignores_placeholders() -> None:
    assert not any(hit.detector == "EMAIL_LIKE" for hit in scan_text("contact [EMAIL]"))
