from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Literal

import pytest

from bca.bq.pull import build_bundle, pull_transcripts, run_preflight
from bca.config import BigQueryConfig, RecipeConfig
from bca.store import CapRule, LeadStatus, Store, UsableRule, open_store

from ..fakes.fake_bq import FakeBqRunner


def recipe() -> RecipeConfig:
    return RecipeConfig("transcripts", "calls", "lead_id", "call_id", "text", "started", "duration", "s")


def store(tmp_path: Path) -> Store:
    result = open_store(tmp_path / "store.sqlite")
    result.create_project("p", "p")
    result.create_job("j", "p", 1)
    return result


def test_preflight_buckets_reconcile_and_errors_raise(tmp_path: Path) -> None:
    db = store(tmp_path)
    for lead in ["1", "2", "3", "4", "5"]:
        db.add_lead("j", lead)
    rows = [
        [
            {"lead_id": "1", "found": True, "calls": 1, "usable_calls": 1, "transcription_failed": 0},
            {"lead_id": "2", "found": False, "calls": 0, "usable_calls": 0, "transcription_failed": 0},
            {"lead_id": "3", "found": True, "calls": 1, "usable_calls": 0, "transcription_failed": 1},
            {"lead_id": "4", "found": True, "calls": 0, "usable_calls": 0, "transcription_failed": 0},
            {"lead_id": "5", "found": True, "calls": 1, "usable_calls": 0, "transcription_failed": 0},
        ]
    ]
    runner = FakeBqRunner(rows, dry=__import__("bca.bq.runner", fromlist=["DryRun"]).DryRun(7, [], []))
    result = run_preflight(recipe(), UsableRule(), "all", None, ["1", "2", "3", "4", "5"], runner, db, "j")
    assert result.buckets == {
        "usable": 1,
        "no_transcript": 1,
        "transcription_failed": 1,
        "too_short": 1,
        "not_found": 1,
    }
    assert db.rows("SELECT status FROM leads WHERE job_id='j' AND lead_id='2'")[0][0] == LeadStatus.not_found
    runner.error = RuntimeError("query failed")
    with pytest.raises(RuntimeError):
        run_preflight(recipe(), UsableRule(), "all", None, ["1"], runner, db, "j")


def test_pull_streams_and_rejects_unapproved_scan(tmp_path: Path) -> None:
    db = store(tmp_path)
    db.add_lead("j", "1")
    from bca.bq.runner import DryRun

    calls = [
        [
            {
                "lead_id": "1",
                "call_id": "c1",
                "call_start": "2026-01-01",
                "duration": 4,
                "transcript_text": "hello",
            }
        ]
    ]
    runner = FakeBqRunner(calls, dry=DryRun(100, [], []))
    cfg = BigQueryConfig("p", reject_bytes=10, max_bytes_billed=1000, warn_bytes=1)
    with pytest.raises(PermissionError):
        pull_transcripts(cfg, recipe(), ["1"], runner, db, "j", tmp_path / "out")
    stats = pull_transcripts(cfg, recipe(), ["1"], runner, db, "j", tmp_path / "out", approved_bytes=100)
    assert stats.calls == 1
    with gzip.open(tmp_path / "out" / "raw" / "1.jsonl.gz", "rt", encoding="utf-8") as stream:
        assert json.loads(stream.readline())["call_id"] == "c1"


@pytest.mark.parametrize("policy", ["drop_short_then_oldest", "keep_newest"])
def test_bundle_cap_numbers_calls_and_records_drops(
    policy: Literal["drop_short_then_oldest", "keep_newest"],
) -> None:
    calls = [
        {"call_id": "a", "call_start": "2026-01-01", "duration": 1, "transcript_text": "aaaa"},
        {"call_id": "b", "call_start": "2026-01-02", "duration": 5, "transcript_text": "bbbb"},
    ]
    bundle = build_bundle("1", calls, CapRule(max_chars=5, policy=policy, drop_short_s=2))
    assert bundle.calls_sent + len(bundle.calls_dropped) == 2
    assert "CALL 1 of 1" in bundle.text
