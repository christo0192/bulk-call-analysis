from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import pytest

from bca.api.production import ProductionBackend
from bca.api.server import RpcError
from bca.outputs.rules import validate_rules
from bca.store import Condition, InScopeRule
from bca.store.spec import _rule
from tests.api.t04_lifecycle import JOB, run_lifecycle_sync


def run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


def test_job_plan_reads_spec_and_preflight_after_propose(tmp_path: Path) -> None:
    lifecycle = run_lifecycle_sync(tmp_path, start_full=False)
    counts = cast(dict[str, object], lifecycle.plan["counts"])
    assert set(counts) == {
        "selected",
        "usable",
        "noTranscript",
        "transcriptionFailed",
        "tooShort",
        "notFound",
        "capped",
        "duplicates",
        "malformed",
    }
    assert counts["selected"] == 8
    assert lifecycle.plan["scanBytes"] == 1


def test_job_plan_rule_summaries_are_plain_english(tmp_path: Path) -> None:
    lifecycle = run_lifecycle_sync(tmp_path, start_full=False)
    summary = lifecycle.backend._rule_summary(
        InScopeRule("all", (Condition("asked_discount", "eq", "yes"),))
    )["summary"]
    assert summary == "In scope if asked_discount is yes"


def test_job_sample_rows_after_sample_ready_are_masked_and_keep_lead_ids(tmp_path: Path) -> None:
    lifecycle = run_lifecycle_sync(tmp_path, start_full=False)
    assert lifecycle.sample["rows"]
    row = cast(dict[str, object], cast(list[object], lifecycle.sample["rows"])[0])
    assert row["lead"] == "1000"
    assert "Synthetic transcript" not in json.dumps(lifecycle.sample)
    # An answer that quotes contact details must come back masked.
    answer = {"answer": "Call 9876543210 or write to synthetic.lead@example.invalid"}
    with lifecycle.backend.store.transaction() as cur:
        cur.execute(
            "UPDATE results SET answer_json=? WHERE job_id=? AND lead_id='1000'", (json.dumps(answer), JOB)
        )
    sample = cast(dict[str, object], run(lifecycle.backend.call("job/sample", {"job": JOB})))
    serialized = json.dumps(sample)
    assert "9876543210" not in serialized and "example.invalid" not in serialized
    masked = next(item for item in cast(list[dict[str, Any]], sample["rows"]) if item["lead"] == "1000")
    assert "[PHONE]" in masked["answer"]["answer"]


def test_job_sample_marks_repairing_leads(tmp_path: Path) -> None:
    lifecycle = run_lifecycle_sync(tmp_path)
    store = lifecycle.backend.store
    store.add_lead(JOB, "repair-1", "queued", in_sample=1)
    attempt = store.start_attempt(JOB, "repair-1", lifecycle.backend.config.models.luna_model)
    assert store.record_result(JOB, "repair-1", attempt, {}, False, ["quote:answer:pending"])
    store.mark_repair_pending(JOB, "repair-1")
    result = run(lifecycle.backend.call("job/sample", {"job": JOB}))
    row = next(item for item in cast(list[dict[str, object]], result["rows"]) if item["lead"] == "repair-1")
    assert row["status"] == "repairing"


def test_job_snapshot_mid_run_has_eta_activity_and_steps(tmp_path: Path) -> None:
    lifecycle = run_lifecycle_sync(tmp_path)
    assert {"progress", "eta", "activity", "steps"} <= lifecycle.running.keys()
    assert cast(dict[str, object], lifecycle.running["activity"])["workers"]
    assert isinstance(lifecycle.running["planApprovedAt"], int)
    assert isinstance(lifecycle.running["sampleApprovedAt"], int)


def test_job_snapshot_steps_mark_waiting_for_you_at_gates(tmp_path: Path) -> None:
    lifecycle = run_lifecycle_sync(tmp_path, start_full=False)
    steps = cast(list[dict[str, object]], lifecycle.plan_snapshot["steps"])
    assert steps[0]["status"] == "waiting_for_you"


def test_job_snapshot_recent_activity_is_plain_english_without_transcript_text(tmp_path: Path) -> None:
    lifecycle = run_lifecycle_sync(tmp_path, start_full=False)
    lifecycle.backend.store.append_activity(JOB, "state", "State changed to planned.")
    result = run(lifecycle.backend.call("job/snapshot", {"job": JOB}))
    assert cast(list[dict[str, object]], result["recent"])[-1]["text"] == "State changed to planned."


def test_job_activity_is_pruned_to_500_rows(tmp_path: Path) -> None:
    lifecycle = run_lifecycle_sync(tmp_path, start_full=False)
    for index in range(501):
        lifecycle.backend.store.append_activity(JOB, "notice", f"Synthetic notice {index}")
    count = lifecycle.backend.store.rows("SELECT COUNT(*) FROM job_activity WHERE job_id=?", (JOB,))[0][0]
    assert count == 500


def test_quota_snapshot_is_persisted_and_projection_is_null_until_movement(tmp_path: Path) -> None:
    lifecycle = run_lifecycle_sync(tmp_path, start_full=False)
    lifecycle.backend.store.append_event(JOB, "quota", {"primary": {"usedPercent": 1}, "takenAt": 1700000000})
    assert lifecycle.backend.store.rows("SELECT kind FROM job_events WHERE job_id=? AND kind='quota'", (JOB,))
    snapshot = run(lifecycle.backend.call("job/snapshot", {"job": JOB}))
    assert snapshot["quota"]["projectedEndPercent"] is None


def test_job_outputs_after_done_has_files_reconciliation_and_issues(tmp_path: Path) -> None:
    lifecycle = run_lifecycle_sync(tmp_path)
    assert lifecycle.outputs["files"]
    assert cast(dict[str, object], lifecycle.outputs["reconciliation"])["addsUp"] is True


def test_job_outputs_reconciliation_maps_versioned_terms_to_public_names() -> None:
    value = ProductionBackend._public_reconciliation(
        {
            "terms": {
                "done": 1,
                "no_transcript": 2,
                "transcription_failed": 3,
                "too_short": 4,
                "not_found": 5,
                "held_back": 6,
                "failed": 7,
                "not_started": 8,
            },
            "selected_distinct_ids": 36,
            "adds_up": True,
        }
    )
    assert value == {
        "selected": 36,
        "read": 1,
        "noTranscript": 2,
        "transcriptionFailed": 3,
        "tooShort": 4,
        "notFound": 5,
        "heldBack": 6,
        "failed": 7,
        "notStarted": 8,
        "addsUp": True,
    }


def test_read_models_unknown_job_is_job_not_found(tmp_path: Path) -> None:
    lifecycle = run_lifecycle_sync(tmp_path, start_full=False)
    for method in ("job/plan", "job/sample", "job/snapshot", "job/outputs"):
        with pytest.raises(RpcError) as error:
            run(lifecycle.backend.call(method, {"job": "missing-job"}))
        assert cast(dict[str, object], error.value.data)["reason"] == "job_not_found"


def test_every_output_path_resolves_through_files_path(tmp_path: Path) -> None:
    lifecycle = run_lifecycle_sync(tmp_path)
    rule = {
        "kind": "review_flag",
        "name": "synthetic_flag",
        "match": "all",
        "conditions": [{"field": "answer", "op": "eq", "value": "synthetic"}],
    }
    run(lifecycle.backend.call("job/rebuildRules", {"job": "job-one", "rules": [rule]}))
    outputs = cast(dict[str, object], run(lifecycle.backend.call("job/outputs", {"job": "job-one"})))
    for item in cast(list[dict[str, object]], outputs["files"]):
        resolved = cast(
            dict[str, object],
            run(lifecycle.backend.call("files/path", {"project": "project-one", "name": item["path"]})),
        )
        assert Path(str(resolved["path"])).is_file()


def test_plan_rule_definitions_round_trip(tmp_path: Path) -> None:
    lifecycle = run_lifecycle_sync(tmp_path)
    definition = {
        "kind": "review_flag",
        "name": "synthetic_flag",
        "match": "all",
        "conditions": [{"field": "answer", "op": "eq", "value": "synthetic"}],
    }
    run(lifecycle.backend.call("job/rebuildRules", {"job": "job-one", "rules": [definition]}))
    plan = cast(dict[str, object], run(lifecycle.backend.call("job/plan", {"job": "job-one"})))
    returned = cast(list[dict[str, object]], plan["rules"])[0]["definition"]
    parsed = (_rule(returned),)
    validate_rules(parsed, lifecycle.backend._read_job_spec("job-one")[1].output_schema)
    assert returned == definition


def test_plan_returns_the_rules_of_the_newest_rebuild(tmp_path: Path) -> None:
    lifecycle = run_lifecycle_sync(tmp_path)
    first = {
        "kind": "review_flag",
        "name": "first_flag",
        "match": "all",
        "conditions": [{"field": "answer", "op": "eq", "value": "synthetic"}],
    }
    second = {
        "kind": "review_flag",
        "name": "second_flag",
        "match": "all",
        "conditions": [{"field": "answer", "op": "eq", "value": "synthetic"}],
    }
    run(lifecycle.backend.call("job/rebuildRules", {"job": "job-one", "rules": [first]}))
    run(lifecycle.backend.call("job/rebuildRules", {"job": "job-one", "rules": [second]}))
    plan = cast(dict[str, object], run(lifecycle.backend.call("job/plan", {"job": "job-one"})))
    assert cast(list[dict[str, object]], plan["rules"])[0]["definition"] == second


def test_snapshot_counts_add_up_with_unusable_leads(tmp_path: Path) -> None:
    lifecycle = run_lifecycle_sync(tmp_path)
    counts = cast(dict[str, object], lifecycle.paused["counts"])
    assert counts["selected"] == sum(
        int(cast(int, counts[name]))
        for name in ("usable", "noTranscript", "transcriptionFailed", "tooShort", "notFound")
    )


def test_snapshot_failure_is_plain_words(tmp_path: Path) -> None:
    lifecycle = run_lifecycle_sync(tmp_path, start_full=False)
    failure = lifecycle.backend._job_failure("job-one", "failed")
    assert failure == {"reason": "failed", "text": "The job failed."}
    unknown = lifecycle.backend._job_failure("job-one", "failed")
    assert unknown is not None
    assert all(char not in unknown["text"] for char in ("/", "\\", "_"))


def test_step_details_use_their_own_counts(tmp_path: Path) -> None:
    lifecycle = run_lifecycle_sync(tmp_path)
    steps = cast(list[dict[str, object]], lifecycle.running["steps"])
    progress = cast(dict[str, object], lifecycle.running["progress"])
    assert steps[4]["detail"] == "2/2"
    # The full run counts the whole job (one count scope), so it matches the progress line and
    # already includes the 2 sample leads, whose results the full run reuses.
    assert steps[7]["detail"] == f"{progress['done']}/{progress['total']}" == "2/8"


def test_progress_event_and_snapshot_use_the_same_scope(tmp_path: Path) -> None:
    lifecycle = run_lifecycle_sync(tmp_path, start_full=False)
    subscription = lifecycle.backend.event_bus.subscribe()
    try:
        lifecycle.backend.jobs.runner("job-one")._publish_progress()
        event = next(item for item in subscription.drain() if item.kind == "counts")
    finally:
        lifecycle.backend.event_bus.unsubscribe(subscription)
    snapshot = cast(dict[str, object], run(lifecycle.backend.call("job/snapshot", {"job": "job-one"})))
    progress = cast(dict[str, object], snapshot["progress"])
    assert event.data["phase"] == progress["phase"]
    assert event.data["done"] == progress["done"]
    assert event.data["total"] == progress["total"]
