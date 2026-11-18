from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from .db import Store


class JobState(StrEnum):
    draft = "draft"
    plan_proposed = "plan_proposed"
    plan_approved = "plan_approved"
    pulling = "pulling"
    masking_sample = "masking_sample"
    sample_running = "sample_running"
    sample_ready = "sample_ready"
    sample_approved = "sample_approved"
    full_running = "full_running"
    paused = "paused"
    stalled = "stalled"
    quote_check = "quote_check"
    building_output = "building_output"
    verifying = "verifying"
    done = "done"
    done_with_issues = "done_with_issues"
    failed = "failed"
    cancelled = "cancelled"


class PauseReason(StrEnum):
    quota = "quota"
    user = "user"
    sleep = "sleep"
    credit = "credit"
    auth = "auth"
    model_unavailable = "model_unavailable"
    error_storm = "error_storm"


class TransitionError(ValueError):
    """Raised when a job state transition is not permitted."""


_TRANSITIONS: dict[JobState, set[JobState]] = {
    JobState.draft: {JobState.plan_proposed, JobState.cancelled},
    JobState.plan_approved: {JobState.pulling},
    JobState.pulling: {JobState.masking_sample, JobState.paused, JobState.failed},
    JobState.masking_sample: {JobState.sample_running, JobState.paused},
    JobState.sample_running: {JobState.sample_ready, JobState.paused, JobState.stalled},
    JobState.sample_approved: {JobState.full_running},
    JobState.full_running: {JobState.paused, JobState.stalled, JobState.quote_check},
    JobState.stalled: {JobState.paused, JobState.full_running, JobState.failed},
    JobState.quote_check: {JobState.building_output, JobState.paused},
    JobState.building_output: {JobState.verifying, JobState.failed},
    JobState.verifying: {JobState.done, JobState.done_with_issues, JobState.failed},
    JobState.paused: {
        JobState.draft,
        JobState.plan_proposed,
        JobState.plan_approved,
        JobState.pulling,
        JobState.masking_sample,
        JobState.sample_running,
        JobState.sample_ready,
        JobState.sample_approved,
        JobState.full_running,
        JobState.stalled,
        JobState.quote_check,
        JobState.building_output,
        JobState.verifying,
    },
}


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _job(store: Store, job_id: str) -> tuple[JobState, str | None]:
    rows = store.rows("SELECT state, pause_reason FROM jobs WHERE id=?", (job_id,))
    if not rows:
        raise TransitionError("job not found")
    return JobState(rows[0][0]), rows[0][1]


def transition(store: Store, job_id: str, to: JobState, reason: str | None = None) -> None:
    current, _ = _job(store, job_id)
    if to not in _TRANSITIONS.get(current, set()):
        raise TransitionError(f"cannot move {current} to {to}")
    with store.lock, store.connection:
        store.connection.execute(
            "UPDATE jobs SET state=?, updated_at=? WHERE id=?", (to, _timestamp(), job_id)
        )
        store.connection.execute(
            "INSERT INTO job_events(job_id, ts, kind, data_json) VALUES (?, ?, 'state', json(?))",
            (job_id, _timestamp(), __import__("json").dumps({"from": current, "to": to, "reason": reason})),
        )


def approve_plan(store: Store, job_id: str, actor: Literal["operator"]) -> None:
    if actor != "operator":
        raise TransitionError("plan approval requires operator")
    current, _ = _job(store, job_id)
    if current != JobState.plan_proposed:
        raise TransitionError("plan is not awaiting approval")
    with store.lock, store.connection:
        store.connection.execute(
            "UPDATE jobs SET state=?, updated_at=? WHERE id=?",
            (JobState.plan_approved, _timestamp(), job_id),
        )
        store.connection.execute(
            "INSERT INTO job_events(job_id, ts, kind, data_json) VALUES (?, ?, 'state', json(?))",
            (
                job_id,
                _timestamp(),
                __import__("json").dumps(
                    {"from": current, "to": JobState.plan_approved, "reason": "plan approved"}
                ),
            ),
        )
        store.connection.execute("UPDATE jobs SET plan_approved_at=? WHERE id=?", (_timestamp(), job_id))


def approve_sample(store: Store, job_id: str, actor: Literal["operator"]) -> None:
    if actor != "operator":
        raise TransitionError("sample approval requires operator")
    if _job(store, job_id)[0] != JobState.sample_ready:
        raise TransitionError("sample is not awaiting approval")
    transition(store, job_id, JobState.sample_approved, "sample approved")
    with store.lock, store.connection:
        store.connection.execute("UPDATE jobs SET sample_approved_at=? WHERE id=?", (_timestamp(), job_id))


def start_full_run(store: Store, job_id: str, actor: Literal["operator"]) -> None:
    if actor != "operator":
        raise TransitionError("full run requires operator")
    if _job(store, job_id)[0] != JobState.sample_approved:
        raise TransitionError("sample is not approved")
    transition(store, job_id, JobState.full_running, "full run started")
    with store.lock, store.connection:
        store.connection.execute("UPDATE jobs SET full_started_at=? WHERE id=?", (_timestamp(), job_id))


def edit_spec(store: Store, job_id: str, spec_version: int) -> None:
    current, _ = _job(store, job_id)
    if current in (
        JobState.plan_proposed,
        JobState.plan_approved,
        JobState.pulling,
        JobState.masking_sample,
        JobState.sample_running,
        JobState.sample_ready,
    ):
        with store.lock, store.connection:
            store.connection.execute(
                "UPDATE jobs SET state='plan_proposed', spec_version=?, "
                "plan_approved_at=NULL, sample_approved_at=NULL, updated_at=? WHERE id=?",
                (spec_version, _timestamp(), job_id),
            )
            store.connection.execute(
                "INSERT INTO job_events(job_id, ts, kind, data_json) VALUES (?, ?, 'state', json(?))",
                (
                    job_id,
                    _timestamp(),
                    __import__("json").dumps(
                        {"from": current, "to": JobState.plan_proposed, "reason": "spec edited"}
                    ),
                ),
            )


def pause(store: Store, job_id: str, reason: PauseReason) -> None:
    current, _ = _job(store, job_id)
    if current not in (
        JobState.pulling,
        JobState.masking_sample,
        JobState.sample_running,
        JobState.full_running,
        JobState.stalled,
        JobState.quote_check,
        JobState.building_output,
        JobState.verifying,
    ):
        raise TransitionError("job is not running")
    with store.lock, store.connection:
        store.connection.execute(
            "UPDATE jobs SET state='paused', pause_reason=?, updated_at=? WHERE id=?",
            (f"{reason}:{current}", _timestamp(), job_id),
        )
        store.connection.execute(
            "INSERT INTO job_events(job_id, ts, kind, data_json) VALUES (?, ?, 'state', json(?))",
            (
                job_id,
                _timestamp(),
                __import__("json").dumps({"from": current, "to": JobState.paused, "reason": reason}),
            ),
        )


def resume(store: Store, job_id: str) -> None:
    current, pause_reason = _job(store, job_id)
    if current != JobState.paused or not pause_reason:
        raise TransitionError("job is not paused")
    target = JobState(pause_reason.split(":", 1)[1])
    transition(store, job_id, target, "resumed")
    with store.lock, store.connection:
        store.connection.execute("UPDATE jobs SET pause_reason=NULL WHERE id=?", (job_id,))
