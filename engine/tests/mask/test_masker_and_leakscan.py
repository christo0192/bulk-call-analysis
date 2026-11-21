from __future__ import annotations

import gzip
import json
from pathlib import Path

from bca.mask.leakscan import calibrate, scan_lead
from bca.mask.masker import MaskingContext, mask_lead_stream, run_masking
from bca.mask.names import Span


class SyntheticPersonNer:
    def persons(self, text: str) -> list[Span]:
        name = "Synthetic Person"
        start = text.find(name)
        return [Span(start, start + len(name))] if start >= 0 else []


def write_lead(path: Path, texts: list[str]) -> None:
    with gzip.open(path, "wt", encoding="utf-8", newline="\n") as handle:
        for text in texts:
            handle.write(json.dumps({"lead_id": "fake-lead", "transcript_text": text}) + "\n")


def read_texts(path: Path) -> list[str]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line)["transcript_text"] for line in handle]


def test_streaming_200_calls_and_counts(tmp_path: Path) -> None:
    raw = tmp_path / "raw.jsonl.gz"
    masked = tmp_path / "masked.jsonl.gz"
    write_lead(raw, [f"Call {i}: 98765-43210" for i in range(200)])
    stats = mask_lead_stream(raw, masked, MaskingContext())
    assert stats.calls == 200
    assert stats.detector_counts["PHONE"] == 200
    assert len(read_texts(masked)) == 200


def test_process_pool_matches_single_process(tmp_path: Path) -> None:
    pairs: list[tuple[Path, Path, MaskingContext]] = []
    for i in range(20):
        raw, out = tmp_path / f"raw-{i}.gz", tmp_path / f"out-{i}.gz"
        write_lead(raw, [f"call {i} 98765-43210"])
        pairs.append((raw, out, MaskingContext()))
    single = run_masking(pairs, workers=1, priority="normal")
    single_text = [read_texts(out) for _, out, _ in pairs]
    for _, out, _ in pairs:
        out.unlink()
    parallel = run_masking(pairs, workers=2, priority="normal")
    assert [item.calls for item in single] == [item.calls for item in parallel]
    assert single_text == [read_texts(out) for _, out, _ in pairs]


def test_leak_scan_holds_back_unmasked_phone_and_ignores_placeholders(tmp_path: Path) -> None:
    planted = tmp_path / "planted.gz"
    clean = tmp_path / "clean.gz"
    write_lead(planted, ["Still exposed: 9876543210", "[PHONE] [EMAIL] [DIGITS]"])
    write_lead(clean, ["The fee is ₹ 45k and the contact is [PHONE]."])
    assert scan_lead(planted).held_back
    assert not scan_lead(clean).hits
    assert calibrate([planted])["PHONE_LIKE"] == 1


def test_leak_scan_detects_handle_and_configures_precision(tmp_path: Path) -> None:
    lead = tmp_path / "handle.gz"
    write_lead(lead, ["contact @synthetic_user"])
    report = scan_lead(lead, precision=frozenset({"medium"}))
    assert [hit.detector for hit in report.hits] == ["HANDLE"]
    assert not report.held_back


def test_leak_scan_ignores_metadata_values(tmp_path: Path) -> None:
    lead = tmp_path / "metadata.gz"
    with gzip.open(lead, "wt", encoding="utf-8", newline="\n") as handle:
        handle.write(
            json.dumps(
                {
                    "lead_id": "600000",
                    "call_id": "550e8400-e29b-41d4-a716-446655440000",
                    "call_start": "2026-09-01T10:00:00+00:00",
                    "duration": "600000",
                    "transcript_text": "A clean masked transcript.",
                }
            )
            + "\n"
        )
    assert not scan_lead(lead).held_back


def test_advisor_and_unknown_string_fields_are_masked(tmp_path: Path) -> None:
    raw = tmp_path / "raw.gz"
    masked = tmp_path / "masked.gz"
    with gzip.open(raw, "wt", encoding="utf-8", newline="\n") as handle:
        handle.write(
            json.dumps(
                {
                    "lead_id": "synthetic-lead",
                    "advisor": "Synthetic Person",
                    "notes": "Call 9876543210",
                    # Not after a greeting: the greeting rule masks one token before NER runs
                    # (tracked separately as a hardening issue).
                    "transcript_text": "Synthetic Person here",
                }
            )
            + "\n"
        )
    mask_lead_stream(raw, masked, MaskingContext(ner=SyntheticPersonNer()))
    with gzip.open(masked, "rt", encoding="utf-8") as handle:
        record = json.loads(handle.readline())
    assert record["advisor"] == "[NAME]"
    assert record["notes"] == "Call [PHONE]"
    assert record["transcript_text"] == "[NAME] here"


def test_leak_scan_holds_back_unknown_string_fields(tmp_path: Path) -> None:
    lead = tmp_path / "unmasked.gz"
    with gzip.open(lead, "wt", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps({"lead_id": "synthetic-lead", "notes": "9876543210"}) + "\n")
    assert scan_lead(lead).held_back


def test_leak_scan_checks_each_masker_free_text_field(tmp_path: Path) -> None:
    leads: list[Path] = []
    for field in ("advisor", "notes", "transcript_text"):
        lead = tmp_path / f"free-text-{field}.gz"
        record = {"lead_id": "clean metadata", field: "Unmasked phone 9876543210"}
        with gzip.open(lead, "wt", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record) + "\n")
        leads.append(lead)
        assert scan_lead(lead).held_back
    assert calibrate(leads)["PHONE_LIKE"] == 3


def test_metadata_values_are_never_masked_or_scanned(tmp_path: Path) -> None:
    raw = tmp_path / "raw.gz"
    masked = tmp_path / "masked.gz"
    record = {
        "lead_id": "600000",
        "call_id": "550e8400-e29b-41d4-a716-446655440000",
        "call_start": "2026-09-01T10:00:00+00:00",
        "duration": "600000",
        "transcript_status": "ok",
        "notes": "clean",
    }
    records = [record, {**record, "duration": 600000}]
    with gzip.open(raw, "wt", encoding="utf-8", newline="\n") as handle:
        for item in records:
            handle.write(json.dumps(item) + "\n")
    mask_lead_stream(raw, masked, MaskingContext())
    with gzip.open(masked, "rt", encoding="utf-8") as handle:
        assert [json.loads(line) for line in handle] == records
    assert not scan_lead(raw).hits


def test_nested_free_text_values_are_masked_and_scanned(tmp_path: Path) -> None:
    raw = tmp_path / "raw.gz"
    masked = tmp_path / "masked.gz"
    with gzip.open(raw, "wt", encoding="utf-8", newline="\n") as handle:
        handle.write(
            json.dumps(
                {
                    "lead_id": "synthetic-lead",
                    "details": {"name": "Synthetic Person", "phones": ["9876543210"]},
                }
            )
            + "\n"
        )
    assert scan_lead(raw).held_back
    mask_lead_stream(raw, masked, MaskingContext(ner=SyntheticPersonNer()))
    with gzip.open(masked, "rt", encoding="utf-8") as handle:
        record = json.loads(handle.readline())
    assert record["details"] == {"name": "[NAME]", "phones": ["[PHONE]"]}
    assert not scan_lead(masked).hits


def test_masker_only_masks_shared_free_text_fields(tmp_path: Path) -> None:
    raw = tmp_path / "raw.gz"
    masked = tmp_path / "masked.gz"
    with gzip.open(raw, "wt", encoding="utf-8", newline="\n") as handle:
        handle.write(
            json.dumps(
                {
                    "call_id": "9876543210",
                    "transcript_text": "Call 9876543210",
                }
            )
            + "\n"
        )
    mask_lead_stream(raw, masked, MaskingContext())
    with gzip.open(masked, "rt", encoding="utf-8") as handle:
        record = json.loads(handle.readline())
    assert record["call_id"] == "9876543210"
    assert record["transcript_text"] == "Call [PHONE]"
