from pathlib import Path

import pytest

from bca.store import (
    CapRule,
    Condition,
    InScopeRule,
    JobSpec,
    LeadSourceIds,
    LeadSourceSql,
    QuoteField,
    ReviewFlagRule,
    SpecError,
    ThresholdGroupRule,
    UsableRule,
    load_latest_spec,
    save_spec,
)


def make_spec(source: LeadSourceSql | LeadSourceIds | None = None) -> JobSpec:
    if source is None:
        source = LeadSourceSql("SELECT 1")
    condition = Condition("status", "eq", "active")
    return JobSpec(
        1,
        "job",
        "project",
        "title",
        "question",
        source,
        calls_scope="window",
        window_start="2025-01-01",
        window_end="2025-01-31",
        usable=UsableRule(10, ("completed",), 30, (1, 2)),
        cap=CapRule(1000, "keep_newest", 60),
        instructions="instructions",
        output_schema={"type": "object", "properties": {"answer": {"type": "string"}}},
        quote_fields=(QuoteField("text", "learner"),),
        rules=(
            InScopeRule("all", (condition,)),
            ThresholdGroupRule("score", "score", (0.5, 0.8), ("low", "mid", "high")),
            ReviewFlagRule("risk", "any", (condition,)),
        ),
        luna_model="model",
        luna_effort="high",
        sample_size=20,
    )


@pytest.mark.parametrize("source", [LeadSourceSql("SELECT 1"), LeadSourceIds("ids.txt", 3)])
def test_spec_round_trip_every_type(source: LeadSourceSql | LeadSourceIds) -> None:
    spec = make_spec(source)
    assert JobSpec.from_dict(spec.to_dict()) == spec
    assert LeadSourceSql.from_dict(LeadSourceSql("SELECT 1").to_dict()) == LeadSourceSql("SELECT 1")
    assert LeadSourceIds.from_dict(LeadSourceIds("ids.txt", 3).to_dict()) == LeadSourceIds("ids.txt", 3)
    assert UsableRule.from_dict(UsableRule().to_dict()) == UsableRule()
    assert CapRule.from_dict(CapRule().to_dict()) == CapRule()
    assert QuoteField.from_dict(QuoteField("text", "any").to_dict()) == QuoteField("text", "any")
    condition = Condition("x", "contains", ["a"])
    assert Condition.from_dict(condition.to_dict()) == condition


@pytest.mark.parametrize(
    ("factory", "payload", "path"),
    [
        (LeadSourceSql.from_dict, {"kind": "sql", "sql": 1}, "lead_source.sql"),
        (LeadSourceIds.from_dict, {"kind": "ids", "ids_file": "x", "count": -1}, "lead_source.count"),
        (UsableRule.from_dict, {"min_chars": -1}, "usable.min_chars"),
        (CapRule.from_dict, {"max_chars": 0}, "cap.max_chars"),
        (QuoteField.from_dict, {"field": 1, "speaker": "any"}, "quote_fields[].field"),
        (Condition.from_dict, {"field": "x", "op": "bad"}, "conditions[].op"),
        (InScopeRule.from_dict, {"kind": "in_scope", "match": "all", "conditions": "bad"}, "conditions"),
        (
            ReviewFlagRule.from_dict,
            {"kind": "review_flag", "name": 1, "match": "all", "conditions": []},
            "rules[].name",
        ),
        (
            ThresholdGroupRule.from_dict,
            {"kind": "threshold_group", "name": "x", "field": "x", "edges": [1], "labels": ["only"]},
            "rules[].labels",
        ),
    ],
)
def test_each_type_reports_bad_field(factory: object, payload: dict[str, object], path: str) -> None:
    with pytest.raises(SpecError, match=path.replace("[", r"\[")):
        factory(payload)  # type: ignore[operator]


@pytest.mark.parametrize(
    ("key", "value", "path"),
    [
        ("calls_scope", "window", "window_start"),
        ("sample_size", 0, "sample_size"),
        ("sample_size", 201, "sample_size"),
        ("output_schema", [], "output_schema"),
    ],
)
def test_job_spec_bad_fields(key: str, value: object, path: str) -> None:
    data = make_spec().to_dict()
    data[key] = value
    if key == "calls_scope":
        data.pop("window_start")
        data.pop("window_end")
    with pytest.raises(SpecError, match=path):
        JobSpec.from_dict(data)


def test_spec_versioning_atomic_write_and_corrupt_latest(tmp_path: Path) -> None:
    spec = make_spec()
    for _ in range(3):
        save_spec(spec, tmp_path)
    loaded, version = load_latest_spec(tmp_path)
    assert version == 3
    assert loaded.spec_version == 3
    assert not list(tmp_path.glob(".spec.*.tmp"))
    (tmp_path / "spec.v3.json").write_text("{", encoding="utf-8")
    with pytest.raises(SpecError, match="spec:"):
        load_latest_spec(tmp_path)
