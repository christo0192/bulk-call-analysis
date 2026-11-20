from pathlib import Path

from bca.store import (
    JobSpec,
    LeadSourceSql,
    LeadStatus,
    approve_plan,
    open_store,
)
from bca.store.lifecycle import JobState, TransitionError
from bca.store.spec import load_latest_spec, save_spec


def test_spec_versioning_and_round_trip(tmp_path: Path) -> None:
    spec = JobSpec(
        spec_version=1,
        job_id="job",
        project_id="project",
        title="Title",
        question="Question",
        lead_source=LeadSourceSql(sql="SELECT 1"),
        output_schema={"type": "object", "properties": {}},
        instructions="",
        luna_model="model",
        luna_effort="high",
    )
    first = save_spec(spec, tmp_path)
    second = save_spec(spec, tmp_path)
    assert first.name == "spec.v1.json"
    assert second.name == "spec.v2.json"
    loaded, version = load_latest_spec(tmp_path)
    assert version == 2
    assert loaded.to_dict() == spec.to_dict() | {"spec_version": 2}


def test_attempt_requeue_rejects_late_result() -> None:
    store = open_store(Path(":memory:"))
    store.create_project("p", "P")
    store.create_job("j", "p", 1)
    store.add_lead("j", "lead", LeadStatus.queued)
    attempt = store.start_attempt("j", "lead", "model")
    assert store.requeue_in_flight("j", "restart") == 1
    assert not store.record_result("j", "lead", attempt, {}, True, [])
    assert store.rows("SELECT kind FROM job_events WHERE job_id='j'")[0][0] == "stale_result"
    store.close()


def test_pause_requeues_open_counted_attempt_as_uncounted() -> None:
    store = open_store(Path(":memory:"))
    store.create_project("p", "P")
    store.create_job("j", "p", 1)
    store.add_lead("j", "lead", LeadStatus.queued)
    attempt = store.start_attempt("j", "lead", "model")

    assert store.requeue_in_flight("j", "user", outcome="interrupted") == 1
    row = store.rows("SELECT outcome, counted FROM attempts WHERE attempt_id=?", (attempt,))[0]
    assert tuple(row) == ("interrupted", 0)
    replacement = store.start_attempt("j", "lead", "model")
    assert replacement != attempt
    store.close()


def test_lifecycle_gate_requires_operator() -> None:
    store = open_store(Path(":memory:"))
    store.create_project("p", "P")
    store.create_job("j", "p", 1, JobState.plan_proposed)
    try:
        approve_plan(store, "j", "not_operator")  # type: ignore[arg-type]
    except TransitionError:
        pass
    else:
        raise AssertionError("non-operator approved the plan")
    approve_plan(store, "j", "operator")
    assert store.rows("SELECT state FROM jobs WHERE id='j'")[0][0] == JobState.plan_approved
    store.close()
