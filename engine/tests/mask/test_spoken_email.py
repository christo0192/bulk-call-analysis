from __future__ import annotations

import gzip
import json
from pathlib import Path

from bca.mask.leakscan import scan_lead, scan_text
from bca.mask.masker import MaskingContext, mask_text


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


def test_leak_scan_holds_back_an_unmasked_spoken_email(tmp_path: Path) -> None:
    assert any(hit.detector == "EMAIL_LIKE" for hit in scan_text("someone at example dot com"))
    path = tmp_path / "lead.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps({"transcript_text": "someone at example dot com"}) + "\n")
    assert scan_lead(path).held_back
