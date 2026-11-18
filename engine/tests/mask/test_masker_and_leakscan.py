from __future__ import annotations

import gzip
import json
from pathlib import Path

from bca.mask.leakscan import calibrate, scan_lead
from bca.mask.masker import MaskingContext, mask_lead_stream, run_masking


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
