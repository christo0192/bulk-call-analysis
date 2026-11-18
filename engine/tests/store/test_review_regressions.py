from __future__ import annotations

# ruff: noqa: E501
import sqlite3
import threading
from pathlib import Path

import pytest

from bca.store import (
    ClaimError,
    JobSpec,
    JobState,
    LeadSourceSql,
    LeadStatus,
    PauseReason,
    SpecError,
    Store,
    TransitionError,
    approve_plan,
    edit_spec,
    load_latest_spec,
    open_store,
    pause,
    reconcile,
    save_spec,
    start_full_run,
    transition,
)


def store_for(path: Path | str = ":memory:", state: JobState = JobState.plan_proposed) -> Store:
    store = open_store(Path(path))
    store.create_project("project", "Synthetic project")
    store.create_job("job", "project", 1, state)
    return store


def spec_data() -> dict[str, object]:
    return {
        "spec_version": 1,
        "job_id": "job",
        "project_id": "project",
        "title": "Synthetic title",
        "question": "Synthetic question",
        "lead_source": {"kind": "sql", "sql": "SELECT 1"},
        "instructions": "",
        "output_schema": {"type": "object", "properties": {"amount": {"type": "number"}}},
        "luna_model": "model",
        "luna_effort": "high",
    }


def make_spec() -> JobSpec:
    return JobSpec.from_dict(spec_data())


def test_transition_cannot_bypass_gates_or_paused_state() -> None:
    for current, target in (
        (JobState.sample_ready, JobState.sample_approved),
        (JobState.sample_approved, JobState.full_running),
        (JobState.pulling, JobState.paused),
    ):
        store = store_for(state=current)
        with pytest.raises(TransitionError):
            transition(store, "job", target)

    store = store_for(state=JobState.full_running)
    transition(store, "job", JobState.stalled)
    with pytest.raises(TransitionError):
        transition(store, "job", JobState.full_running)
    with pytest.raises(TransitionError):
        transition(store, "job", JobState.paused)
    paused = store_for(state=JobState.pulling)
    pause(paused, "job", PauseReason.user)
    with pytest.raises(TransitionError):
        transition(paused, "job", JobState.pulling)


def test_sample_stall_recovers_only_to_sample_running() -> None:
    store = store_for(state=JobState.sample_running)
    transition(store, "job", JobState.stalled, "silence")
    with pytest.raises(TransitionError):
        transition(store, "job", JobState.full_running)
    transition(store, "job", JobState.sample_running, "recovered")


def test_edit_spec_resets_pre_full_pause_and_rejects_full_run() -> None:
    store = store_for(state=JobState.plan_proposed)
    approve_plan(store, "job", "operator", expected_spec_version=1)
    transition(store, "job", JobState.pulling)
    pause(store, "job", PauseReason.user)
    edit_spec(store, "job", 2)
    row = store.rows("SELECT state, spec_version, plan_approved_at, sample_approved_at FROM jobs")[0]
    assert tuple(row) == (JobState.plan_proposed, 2, None, None)

    running = store_for(state=JobState.full_running)
    with pytest.raises(TransitionError):
        edit_spec(running, "job", 2)


def test_interleaved_edit_cannot_leave_stale_approval(tmp_path: Path) -> None:
    path = tmp_path / "interleave.sqlite"
    store = store_for(path, state=JobState.sample_approved)
    store.connection.execute("UPDATE jobs SET plan_approved_at='old', sample_approved_at='old'")
    store.connection.commit()
    editor = open_store(path)
    original = store.rows("SELECT state, spec_version FROM jobs")[0]
    assert tuple(original) == (JobState.sample_approved, 1)
    edit_spec(editor, "job", 2)
    with pytest.raises(TransitionError):
        start_full_run(store, "job", "operator", expected_spec_version=1)
    row = store.rows("SELECT state, spec_version, plan_approved_at, sample_approved_at FROM jobs")[0]
    assert tuple(row) == (JobState.plan_proposed, 2, None, None)


def test_approval_rejects_wrong_expected_spec_version() -> None:
    store = store_for()
    with pytest.raises(TransitionError):
        approve_plan(store, "job", "operator", expected_spec_version=2)
    assert store.rows("SELECT state FROM jobs")[0][0] == JobState.plan_proposed


def test_edit_during_sample_supersedes_attempt_and_rejects_old_result() -> None:
    store = store_for(state=JobState.sample_running)
    store.add_lead("job", "lead", LeadStatus.queued, in_sample=1)
    attempt = store.start_attempt("job", "lead", "model")
    edit_spec(store, "job", 2)
    row = store.rows("SELECT ended_at, outcome, counted FROM attempts WHERE attempt_id=?", (attempt,))[0]
    assert row[0] is not None and tuple(row[1:]) == ("superseded_by_spec", 0)
    assert not store.record_result("job", "lead", attempt, {}, True, [])


def test_two_workers_cannot_claim_and_ghost_lead_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "claims.sqlite"
    first = store_for(path)
    first.add_lead("job", "lead", LeadStatus.queued)
    second = open_store(path)
    first_attempt = first.start_attempt("job", "lead", "model")
    with pytest.raises(ClaimError):
        second.start_attempt("job", "lead", "model")
    with pytest.raises(ClaimError):
        first.start_attempt("job", "ghost", "model")
    assert first_attempt


def test_superseded_attempt_is_not_counted_against_retry_limit() -> None:
    store = store_for()
    store.add_lead("job", "lead", LeadStatus.queued)
    first = store.start_attempt("job", "lead", "model")
    store.connection.execute(
        "UPDATE attempts SET ended_at='now', outcome='superseded', counted=0 WHERE attempt_id=?", (first,)
    )
    store.connection.execute("UPDATE leads SET status='queued' WHERE lead_id='lead'")
    store.connection.commit()
    second = store.start_attempt("job", "lead", "model")
    assert store.rows("SELECT counted FROM attempts WHERE attempt_id=?", (first,))[0][0] == 0
    store.fail_attempt(second, "temporary", counted=True, max_attempts=2)
    assert store.rows("SELECT status FROM leads WHERE lead_id='lead'")[0][0] == LeadStatus.queued


def test_stale_result_after_other_store_supersedes_attempt_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "stale.sqlite"
    first = store_for(path)
    first.add_lead("job", "lead", LeadStatus.queued)
    second = open_store(path)
    attempt = first.start_attempt("job", "lead", "model")
    assert second.requeue_in_flight("job", "restart") == 1
    replacement = second.start_attempt("job", "lead", "model")
    assert not first.record_result("job", "lead", attempt, {"stale": True}, True, [])
    assert second.record_result("job", "lead", replacement, {"fresh": True}, True, [])


def v1_database(path: Path, *, duplicate: bool = False) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE projects(id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE jobs(id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), state TEXT NOT NULL,
            pause_reason TEXT, spec_version INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            plan_approved_at TEXT, sample_approved_at TEXT, full_started_at TEXT, finished_at TEXT);
        CREATE TABLE job_events(id INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), ts TEXT NOT NULL,
            kind TEXT NOT NULL, data_json TEXT NOT NULL);
        CREATE TABLE leads(job_id TEXT NOT NULL REFERENCES jobs(id), lead_id TEXT NOT NULL, status TEXT NOT NULL,
            reason TEXT, in_sample INTEGER NOT NULL DEFAULT 0, calls_pulled INTEGER NOT NULL DEFAULT 0,
            calls_sent INTEGER NOT NULL DEFAULT 0, calls_dropped_cap INTEGER NOT NULL DEFAULT 0,
            chars INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX leads_job_lead ON leads(job_id, lead_id);
        CREATE TABLE attempts(attempt_id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), lead_id TEXT NOT NULL,
            started_at TEXT NOT NULL, ended_at TEXT, outcome TEXT, error_code TEXT, counted INTEGER NOT NULL DEFAULT 1,
            model_requested TEXT NOT NULL, model_used TEXT, rerouted_from TEXT, input_tokens INTEGER, output_tokens INTEGER);
        CREATE TABLE results(job_id TEXT NOT NULL, lead_id TEXT NOT NULL, attempt_id TEXT NOT NULL, answer_json TEXT NOT NULL,
            quotes_ok INTEGER NOT NULL, flags_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(job_id, lead_id));
        CREATE TABLE id_intake(job_id TEXT PRIMARY KEY REFERENCES jobs(id), raw_count INTEGER NOT NULL, distinct_count INTEGER NOT NULL,
            duplicates INTEGER NOT NULL, malformed INTEGER NOT NULL);
        INSERT INTO projects VALUES ('project', 'Synthetic project', 'now');
        INSERT INTO jobs(id, project_id, state, spec_version, created_at, updated_at)
            VALUES ('job', 'project', 'draft', 1, 'now', 'now');
        INSERT INTO leads(job_id, lead_id, status) VALUES ('job', 'one', 'queued');
        PRAGMA user_version=1;
        """
    )
    if duplicate:
        connection.execute("INSERT INTO leads(job_id, lead_id, status) VALUES ('job', 'one', 'done')")
    connection.commit()
    connection.close()


def test_v1_migration_preserves_rows_and_duplicate_failure_rolls_back(tmp_path: Path) -> None:
    valid = tmp_path / "valid.sqlite"
    v1_database(valid)
    store = open_store(valid)
    assert store.rows("SELECT lead_id FROM leads")[0][0] == "one"
    assert store.rows("PRAGMA user_version")[0][0] == 3
    store.close()

    invalid = tmp_path / "invalid.sqlite"
    v1_database(invalid, duplicate=True)
    with pytest.raises(RuntimeError, match="duplicate"):
        open_store(invalid)
    connection = sqlite3.connect(invalid)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
    assert connection.execute("SELECT count(*) FROM leads").fetchone()[0] == 2
    connection.close()
    with pytest.raises(RuntimeError, match="duplicate"):
        open_store(invalid)


def test_reconcile_db_path_reports_missing_results_orphans_unknown_and_final_in_flight() -> None:
    store = store_for()
    store.add_lead("job", "done-without-result", LeadStatus.done)
    store.add_lead("job", "orphan-result", LeadStatus.queued)
    store.add_lead("job", "unknown", "mystery")
    store.add_lead("job", "in-flight", LeadStatus.queued)
    store.start_attempt("job", "in-flight", "model")
    store.connection.execute(
        "INSERT INTO results VALUES ('job', 'orphan-result', 'missing-attempt', '{}', 1, '[]', 'now', 1)"
    )
    store.connection.commit()
    result = reconcile(store, "job", final=True)
    assert not result.ok
    assert "results do not equal done leads" in result.problems
    assert any("unknown lead status" in problem for problem in result.problems)
    assert "pending lead in-flight" in result.problems


@pytest.mark.parametrize(
    "rule",
    [
        {
            "kind": "threshold_group",
            "name": "group",
            "field": "amount",
            "edges": [2, 1],
            "labels": ["a", "b", "c"],
        },
        {
            "kind": "threshold_group",
            "name": "group",
            "field": "amount",
            "edges": [1, 1],
            "labels": ["a", "b", "c"],
        },
        {
            "kind": "threshold_group",
            "name": "group",
            "field": "amount",
            "edges": [float("nan")],
            "labels": ["a", "b"],
        },
        {"kind": "threshold_group", "name": "group", "field": "amount", "edges": [1], "labels": ["a", "a"]},
        {"kind": "threshold_group", "name": "group", "field": "amount", "edges": [1], "labels": ["", "b"]},
        {
            "kind": "review_flag",
            "name": "group",
            "match": "any",
            "conditions": [{"field": "amount", "op": "eq", "value": 1}],
        },
    ],
)
def test_invalid_threshold_labels_edges_and_duplicate_rule_names_are_rejected(
    rule: dict[str, object],
) -> None:
    data = spec_data()
    data["rules"] = [
        rule,
        {
            "kind": "review_flag",
            "name": "group",
            "match": "all",
            "conditions": [{"field": "amount", "op": "eq", "value": 1}],
        },
    ]
    with pytest.raises(SpecError):
        JobSpec.from_dict(data)


@pytest.mark.parametrize(
    ("condition",),
    [
        ({"field": "amount", "op": "in", "value": 1},),
        ({"field": "amount", "op": "gt", "value": None},),
        ({"field": "amount", "op": "lt", "value": [1, 2]},),
    ],
)
def test_condition_value_matches_operator(condition: dict[str, object]) -> None:
    data = spec_data()
    data["rules"] = [{"kind": "in_scope", "match": "all", "conditions": [condition]}]
    with pytest.raises(SpecError):
        JobSpec.from_dict(data)


def test_empty_rules_conditions_and_spec_shape_are_rejected() -> None:
    data = spec_data()
    data["rules"] = [{"kind": "in_scope", "match": "any", "conditions": []}]
    with pytest.raises(SpecError):
        JobSpec.from_dict(data)
    data = spec_data()
    data["output_schema"] = {"type": "object", "properties": "not-an-object"}
    with pytest.raises(SpecError):
        JobSpec.from_dict(data)
    data = spec_data()
    data["quote_fields"] = [{"field": "missing", "speaker": "learner"}]
    with pytest.raises(SpecError):
        JobSpec.from_dict(data)
    data = spec_data() | {"unknown_top_level": True}
    with pytest.raises(SpecError):
        JobSpec.from_dict(data)


@pytest.mark.parametrize(
    "source",
    [
        {"kind": "ids", "ids_file": "C:/absolute", "count": 1},
        {"kind": "ids", "ids_file": "../ids", "count": 1},
    ],
)
def test_ids_file_must_be_relative_without_parent(source: dict[str, object]) -> None:
    data = spec_data()
    data["lead_source"] = source
    with pytest.raises(SpecError):
        JobSpec.from_dict(data)


def test_save_spec_validates_directly_built_spec_and_ignores_stray_copy(tmp_path: Path) -> None:
    save_spec(make_spec(), tmp_path)
    (tmp_path / "spec.v2 - Copy.json").write_text((tmp_path / "spec.v1.json").read_text(), encoding="utf-8")
    (tmp_path / "Spec.V3.json").write_text((tmp_path / "spec.v1.json").read_text(), encoding="utf-8")
    path = save_spec(make_spec(), tmp_path)
    assert path.name == "spec.v2.json"
    assert load_latest_spec(tmp_path)[1] == 2
    invalid = JobSpec(
        1, "job", "project", "title", "question", LeadSourceSql("SELECT 1"), luna_model="m", luna_effort="h"
    )
    with pytest.raises(SpecError):
        save_spec(invalid, tmp_path / "invalid")
    assert not (tmp_path / "invalid" / "spec.v1.json").exists()


def test_concurrent_save_spec_assigns_eight_distinct_versions(tmp_path: Path) -> None:
    barrier = threading.Barrier(8)
    results: list[Path] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait()
            results.append(save_spec(make_spec(), tmp_path))
        except BaseException as exc:  # pragma: no cover - assertion below reports it
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert {path.name for path in results} == {f"spec.v{i}.json" for i in range(1, 9)}


@pytest.mark.parametrize(
    "changes",
    [
        {"calls_scope": "window", "window_start": "not-a-date", "window_end": "2026-01-01"},
        {"calls_scope": "window", "window_start": "2026-12-31", "window_end": "2026-01-01"},
        {"calls_scope": "all", "window_start": "2026-01-01", "window_end": "2026-01-02"},
    ],
)
def test_window_scope_dates_are_consistent(changes: dict[str, object]) -> None:
    with pytest.raises(SpecError):
        JobSpec.from_dict(spec_data() | changes)
