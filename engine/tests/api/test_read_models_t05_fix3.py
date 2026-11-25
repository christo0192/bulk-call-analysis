from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from bca.api.server import RpcError
from bca.outputs.rules import validate_rules
from bca.store import (
    Condition,
    InScopeRule,
    JobSpec,
    LeadSourceSql,
    load_latest_spec,
    save_spec,
)
from bca.store.lifecycle import JobState, start_full_run, transition
from bca.store.spec import _rule
from tests.api.t04_lifecycle import JOB, run_lifecycle_sync


def run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


OUTPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "score": {"type": "number"},
    },
}


def _spec(job_id: str = "synthetic-job", project_id: str = "synthetic-project") -> JobSpec:
    return JobSpec(
        1,
        job_id,
        project_id,
        "Synthetic job",
        "Synthetic question",
        LeadSourceSql("SELECT 'synthetic' AS lead_id"),
        output_schema=OUTPUT_SCHEMA,
        luna_model="synthetic-model",
        luna_effort="low",
    )


@pytest.mark.parametrize("kind", ["in_scope", "threshold_group", "review_flag"])
@pytest.mark.parametrize(
    "op, value",
    [
        ("eq", "synthetic"),
        ("ne", "synthetic"),
        ("in", ["synthetic", "other"]),
        ("not_in", ["other", "another"]),
        ("lt", 10),
        ("le", 10),
        ("gt", 1),
        ("ge", 1),
        ("is_null", None),
        ("not_null", None),
        ("contains", "synt"),
    ],
)
def test_every_rule_kind_round_trips(kind: str, op: str, value: object) -> None:
    condition: dict[str, object] = {
        "field": "score" if op in {"lt", "le", "gt", "ge"} else "answer",
        "op": op,
    }
    if op not in {"is_null", "not_null"}:
        condition["value"] = value
    if kind == "in_scope":
        definition: dict[str, object] = {"kind": kind, "match": "all", "conditions": [condition]}
    elif kind == "review_flag":
        definition = {
            "kind": kind,
            "name": "synthetic_flag",
            "match": "all",
            "conditions": [condition],
        }
    else:
        definition = {
            "kind": kind,
            "name": "synthetic_band",
            "field": "score",
            "edges": [1, 10],
            "labels": ["low", "mid", "high"],
        }

    rule = _rule(definition)
    encoded = json.dumps(rule.to_dict())
    decoded = _rule(json.loads(encoded))
    assert decoded == rule
    validate_rules((decoded,), OUTPUT_SCHEMA)
    if op in {"is_null", "not_null"} and kind != "threshold_group":
        conditions = cast(list[dict[str, object]], rule.to_dict()["conditions"])
        assert "value" not in conditions[0]


def test_spec_with_is_null_condition_loads(tmp_path: Path) -> None:
    spec = replace(
        _spec(),
        rules=(InScopeRule("all", (Condition("answer", "is_null"),)),),
    )
    save_spec(spec, tmp_path)
    loaded, _ = load_latest_spec(tmp_path)
    assert loaded == spec


def test_plan_definitions_resend_unchanged(tmp_path: Path) -> None:
    lifecycle = run_lifecycle_sync(tmp_path)
    definitions = [
        {
            "kind": "in_scope",
            "match": "all",
            "conditions": [{"field": "answer", "op": "is_null"}],
        },
        {
            "kind": "review_flag",
            "name": "synthetic_flag",
            "match": "all",
            "conditions": [{"field": "answer", "op": "not_null"}],
        },
    ]
    run(lifecycle.backend.call("job/rebuildRules", {"job": JOB, "rules": definitions}))
    plan = cast(dict[str, object], run(lifecycle.backend.call("job/plan", {"job": JOB})))
    resend = [cast(dict[str, object], item)["definition"] for item in cast(list[object], plan["rules"])]
    run(lifecycle.backend.call("job/rebuildRules", {"job": JOB, "rules": resend}))
    after = cast(dict[str, object], run(lifecycle.backend.call("job/plan", {"job": JOB})))
    assert [
        cast(dict[str, object], item)["definition"] for item in cast(list[object], after["rules"])
    ] == resend


def test_unreadable_rebuild_rules_is_an_error(tmp_path: Path) -> None:
    lifecycle = run_lifecycle_sync(tmp_path)
    rules = [{"kind": "in_scope", "match": "all", "conditions": [{"field": "answer", "op": "is_null"}]}]
    run(lifecycle.backend.call("job/rebuildRules", {"job": JOB, "rules": rules}))
    folder, _, version, _ = lifecycle.backend._read_job_spec(JOB)
    newest = max((folder / "output").glob(f"results.v{version}.rebuild*.rules.json"))
    newest.write_text("{invalid", encoding="utf-8", newline="\n")
    with pytest.raises(RpcError) as error:
        run(lifecycle.backend.call("job/plan", {"job": JOB}))
    assert error.value.code == -32001
    assert cast(dict[str, object], error.value.data)["reason"] == "rules_unreadable"


@pytest.mark.parametrize(
    "reason, text",
    [
        ("pull_failed", "Pulling the transcripts failed."),
        ("recovery_failed", "The job could not recover after a restart."),
        ("masking_failed", "Masking the transcripts failed."),
        ("no leads resolved from the lead source", "No leads matched the lead source."),
        ("background task failed", "A background step failed."),
        ("synthetic unknown reason", "The job stopped and needs attention."),
    ],
)
def test_stalled_snapshot_explains_the_reason(tmp_path: Path, reason: str, text: str) -> None:
    lifecycle = run_lifecycle_sync(tmp_path, start_full=False)
    start_full_run(lifecycle.backend.store, JOB, "operator")
    running_status = cast(dict[str, object], run(lifecycle.backend.call("job/status", {"job": JOB})))
    assert running_status["stall"] is None
    transition(lifecycle.backend.store, JOB, JobState.stalled, reason)
    expected = {"reason": reason, "text": text}
    snapshot = cast(dict[str, object], run(lifecycle.backend.call("job/snapshot", {"job": JOB})))
    status = cast(dict[str, object], run(lifecycle.backend.call("job/status", {"job": JOB})))
    assert snapshot["stall"] == expected
    assert status["stall"] == expected
