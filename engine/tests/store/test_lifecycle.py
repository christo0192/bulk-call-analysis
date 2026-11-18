import json
from pathlib import Path

import pytest

from bca.store import JobState, PauseReason, Store, TransitionError, open_store
from bca.store.lifecycle import (
    _TRANSITIONS,
    approve_plan,
    approve_sample,
    edit_spec,
    pause,
    resume,
    start_full_run,
    transition,
)


def job(state: JobState) -> Store:
    store = open_store(Path(":memory:"))
    store.create_project("p", "Project")
    store.create_job("j", "p", 1, state)
    return store


def test_every_allowed_transition_and_representative_disallowed() -> None:
    for current, targets in _TRANSITIONS.items():
        for target in targets:
            store = job(current)
            transition(store, "j", target, "test")
            assert store.rows("SELECT state FROM jobs WHERE id='j'")[0][0] == target
    store = job(JobState.draft)
    with pytest.raises(TransitionError):
        transition(store, "j", JobState.done)


def test_operator_gates_and_spec_edit_clear_approvals() -> None:
    store = job(JobState.plan_proposed)
    with pytest.raises(TransitionError):
        approve_plan(store, "j", "system")  # type: ignore[arg-type]
    approve_plan(store, "j", "operator")
    edit_spec(store, "j", 2)
    row = store.rows("SELECT state, spec_version, plan_approved_at, sample_approved_at FROM jobs")[0]
    assert tuple(row) == ("plan_proposed", 2, None, None)
    store = job(JobState.sample_ready)
    with pytest.raises(TransitionError):
        approve_sample(store, "j", "system")  # type: ignore[arg-type]
    approve_sample(store, "j", "operator")
    with pytest.raises(TransitionError):
        start_full_run(store, "j", "system")  # type: ignore[arg-type]


@pytest.mark.parametrize("state", [JobState.full_running, JobState.sample_running])
def test_pause_resume_returns_to_previous_running_state(state: JobState) -> None:
    store = job(state)
    pause(store, "j", PauseReason.user)
    assert store.rows("SELECT state FROM jobs")[0][0] == JobState.paused
    resume(store, "j")
    assert store.rows("SELECT state FROM jobs")[0][0] == state


def test_transitions_write_state_event_with_reason() -> None:
    store = job(JobState.draft)
    transition(store, "j", JobState.plan_proposed, "because")
    event = store.rows("SELECT kind, data_json FROM job_events")[0]
    assert event[0] == "state"
    assert json.loads(event[1]) == {"from": "draft", "to": "plan_proposed", "reason": "because"}
