from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Literal

import pytest

from bca.bq.pull import build_bundle, pull_transcripts, run_preflight
from bca.config import BigQueryConfig, RecipeConfig
from bca.monitor.progress import counts
from bca.store import CapRule, LeadStatus, Store, UsableRule, open_store, reconcile

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
            {
                "lead_id": "1",
                "found": True,
                "calls": 1,
                "transcript_calls": 1,
                "usable_calls": 1,
                "transcription_failed": 0,
            },
            {
                "lead_id": "2",
                "found": False,
                "calls": 0,
                "transcript_calls": 0,
                "usable_calls": 0,
                "transcription_failed": 0,
            },
            {
                "lead_id": "3",
                "found": True,
                "calls": 1,
                "transcript_calls": 1,
                "usable_calls": 0,
                "transcription_failed": 1,
            },
            {
                "lead_id": "4",
                "found": True,
                "calls": 1,
                "transcript_calls": 0,
                "usable_calls": 0,
                "transcription_failed": 0,
            },
            {
                "lead_id": "5",
                "found": True,
                "calls": 1,
                "transcript_calls": 1,
                "usable_calls": 0,
                "transcription_failed": 0,
            },
        ]
    ]
    runner = FakeBqRunner(rows, dry=__import__("bca.bq.runner", fromlist=["DryRun"]).DryRun(7, [], []))
    result = run_preflight(
        recipe(), UsableRule(), "all", None, ["1", "2", "3", "4", "5"], runner, db, "j", BigQueryConfig("p")
    )
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
        run_preflight(recipe(), UsableRule(), "all", None, ["1"], runner, db, "j", BigQueryConfig("p"))


def test_too_short_untranscribed_call_is_not_a_cap_drop(tmp_path: Path) -> None:
    db = store(tmp_path)
    db.add_lead("j", "short")
    from bca.bq.runner import DryRun

    runner = FakeBqRunner(
        [
            [
                {
                    "lead_id": "short",
                    "found": True,
                    "calls": 2,
                    "transcript_calls": 1,
                    "usable_calls": 0,
                    "transcription_failed": 0,
                }
            ]
        ],
        dry=DryRun(1, [], []),
    )
    run_preflight(recipe(), UsableRule(), "all", None, ["short"], runner, db, "j", BigQueryConfig("p"))

    row = db.rows("SELECT status, calls_pulled, calls_dropped_cap FROM leads WHERE lead_id='short'")[0]
    assert tuple(row) == (LeadStatus.too_short, 0, 0)
    assert db.rows("SELECT calls_dropped_unusable FROM leads WHERE lead_id='short'")[0][0] == 2
    assert counts(db, "j").capped == 0
    assert reconcile(db, "j", final=True).ok


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


def test_pull_requests_only_usable_ids(tmp_path: Path) -> None:
    db = store(tmp_path)
    for lead_id in ("1", "3", "5"):
        db.add_lead("j", lead_id)
    from bca.bq.runner import DryRun

    runner = FakeBqRunner(
        [
            [
                {"lead_id": "1", "call_id": "c1", "transcript_text": "one"},
                {"lead_id": "3", "call_id": "c3", "transcript_text": "three"},
                {"lead_id": "5", "call_id": "c5", "transcript_text": "five"},
            ]
        ],
        dry=DryRun(1, [], []),
    )
    stats = pull_transcripts(BigQueryConfig("p"), recipe(), ["1", "5"], runner, db, "j", tmp_path / "out")

    assert stats.calls == 2
    assert not (tmp_path / "out" / "raw" / "3.jsonl.gz").exists()
    assert "IN UNNEST(@lead_ids)" in str(runner.calls[0][0])


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


def test_millisecond_durations_drive_cap_and_headers() -> None:
    calls = [
        {"call_id": "long_old", "call_start": "2026-01-01", "duration": 1_200_000, "transcript_text": "aaaa"},
        {"call_id": "long_new", "call_start": "2026-01-02", "duration": 1_200_000, "transcript_text": "bbbb"},
        {"call_id": "short", "call_start": "2026-01-03", "duration": 60_000, "transcript_text": "ccccc"},
    ]
    bundle = build_bundle("1", calls, CapRule(10, "drop_short_then_oldest", 180), "ms")
    assert bundle.calls_dropped == ["short"]
    assert "1200.0 s" in bundle.text


def test_empty_lead_is_not_marked_pulled(tmp_path: Path) -> None:
    db = store(tmp_path)
    db.add_lead("j", "1")
    db.add_lead("j", "2")
    from bca.bq.runner import DryRun

    runner = FakeBqRunner([[{"lead_id": "1", "call_id": "c", "transcript_text": "x"}]], DryRun(1, [], []))
    pull_transcripts(BigQueryConfig("p"), recipe(), ["1", "2"], runner, db, "j", tmp_path / "out")
    statuses = db.rows("SELECT lead_id, status, calls_pulled FROM leads ORDER BY lead_id")
    assert [(row[0], row[1], row[2]) for row in statuses] == [("1", "pulled", 1), ("2", "no_transcript", 0)]


def test_preflight_rejects_unapproved_scan_before_execution(tmp_path: Path) -> None:
    db = store(tmp_path)
    db.add_lead("j", "1")
    from bca.bq.runner import DryRun

    runner = FakeBqRunner([], DryRun(11, [], []))
    with pytest.raises(PermissionError):
        run_preflight(
            recipe(), UsableRule(), "all", None, ["1"], runner, db, "j", BigQueryConfig("p", reject_bytes=10)
        )
    assert len(runner.calls) == 1
