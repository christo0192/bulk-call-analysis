from __future__ import annotations

# ruff: noqa: E501
import json
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


def public_state(internal: str | JobState) -> str:
    value = internal.value if isinstance(internal, JobState) else internal
    if value in {"draft", "plan_proposed"}:
        return "planned"
    if value in {
        "pulling",
        "masking_sample",
        "sample_running",
        "full_running",
        "quote_check",
        "building_output",
        "verifying",
    }:
        return "running"
    return value


class PauseReason(StrEnum):
    quota = "quota"
    user = "user"
    sleep = "sleep"
    credit = "credit"
    auth = "auth"
    model_unavailable = "model_unavailable"
    error_storm = "error_storm"
    network = "network"
    spend_control = "spend_control"
    power = "power"


class TransitionError(ValueError):
    """Raised when a job state transition is not permitted."""


_TRANSITIONS: dict[JobState, set[JobState]] = {
    JobState.draft: {JobState.plan_proposed, JobState.cancelled},
    JobState.plan_approved: {JobState.pulling},
    JobState.pulling: {JobState.masking_sample, JobState.failed, JobState.stalled},
    JobState.masking_sample: {JobState.sample_running, JobState.stalled},
    JobState.sample_running: {JobState.sample_ready, JobState.stalled},
    JobState.sample_ready: set(),
    JobState.sample_approved: set(),
    JobState.full_running: {JobState.stalled, JobState.quote_check},
    JobState.stalled: {
        JobState.pulling,
        JobState.masking_sample,
        JobState.sample_running,
        JobState.failed,
    },
    JobState.quote_check: {JobState.building_output, JobState.stalled},
    JobState.building_output: {JobState.verifying, JobState.failed, JobState.stalled},
    JobState.verifying: {JobState.done, JobState.done_with_issues, JobState.failed, JobState.stalled},
}


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _job(store: Store, job_id: str) -> tuple[JobState, str | None, JobState | None, int]:
    rows = store.rows(
        "SELECT state, pause_reason, resume_state, spec_version FROM jobs WHERE id=?", (job_id,)
    )
    if not rows:
        raise TransitionError("job not found")
    return JobState(rows[0][0]), rows[0][1], JobState(rows[0][2]) if rows[0][2] else None, int(rows[0][3])


def _event(cur: object, job_id: str, current: JobState, target: JobState, reason: str | None) -> None:
    assert hasattr(cur, "execute")
    cur.execute(
        "INSERT INTO job_events(job_id, ts, kind, data_json) VALUES (?, ?, 'state', ?)",
        (job_id, _timestamp(), json.dumps({"from": current, "to": target, "reason": reason})),
    )
    cur.execute(
        "INSERT INTO job_activity(job_id, ts, kind, text) VALUES (?, ?, 'state', ?)",
        (job_id, _timestamp(), f"State changed from {current} to {target}."),
    )
    cur.execute(
        "DELETE FROM job_activity WHERE job_id=? AND id NOT IN "
        "(SELECT id FROM job_activity WHERE job_id=? ORDER BY id DESC LIMIT 500)",
        (job_id, job_id),
    )


def transition(store: Store, job_id: str, to: JobState, reason: str | None = None) -> None:
    current, _, resume_state, _ = _job(store, job_id)
    if (
        to in {JobState.plan_approved, JobState.sample_approved, JobState.full_running, JobState.paused}
        or current == JobState.paused
    ):
        raise TransitionError("gated and paused transitions require their dedicated operation")
    if current == JobState.stalled and to != resume_state and to != JobState.failed:
        raise TransitionError(f"stalled job can only recover to {resume_state}")
    if to not in _TRANSITIONS.get(current, set()):
        raise TransitionError(f"cannot move {current} to {to}")
    with store.transaction() as cur:
        changed = cur.execute(
            "UPDATE jobs SET state=?, resume_state=?, updated_at=?, finished_at=CASE WHEN ? IN ('done','done_with_issues','failed','cancelled') THEN ? ELSE finished_at END WHERE id=? AND state=?",
            (
                to,
                current if to == JobState.stalled else None,
                _timestamp(),
                to.value,
                _timestamp(),
                job_id,
                current,
            ),
        )
        if changed.rowcount != 1:
            raise TransitionError("job changed before transition")
        _event(cur, job_id, current, to, reason)
    store.notify_transition(job_id, current.value, to.value, reason)


def _gate(
    store: Store,
    job_id: str,
    actor: Literal["operator"],
    expected: int | None,
    source: JobState,
    target: JobState,
    timestamp_column: str,
    message: str,
) -> None:
    if actor != "operator":
        raise TransitionError(f"{message} requires operator")
    with store.transaction() as cur:
        clauses = " AND spec_version=?" if expected is not None else ""
        args_list: list[object] = [
            target,
            _timestamp(),
            _timestamp(),
            _timestamp()
            if target in {JobState.done, JobState.done_with_issues, JobState.failed, JobState.cancelled}
            else None,
            job_id,
            source,
        ]
        if expected is not None:
            args_list.append(expected)
        args = tuple(args_list)
        changed = cur.execute(
            f"UPDATE jobs SET state=?, {timestamp_column}=?, updated_at=?, finished_at=? WHERE id=? AND state=?{clauses}",
            args,
        )
        if changed.rowcount != 1:
            raise TransitionError(f"{message} is not valid for this job or spec version")
        _event(cur, job_id, source, target, message)
    store.notify_transition(job_id, source.value, target.value, message)


def approve_plan(
    store: Store, job_id: str, actor: Literal["operator"], expected_spec_version: int | None = None
) -> None:
    _gate(
        store,
        job_id,
        actor,
        expected_spec_version,
        JobState.plan_proposed,
        JobState.plan_approved,
        "plan_approved_at",
        "plan approved",
    )


def approve_sample(
    store: Store, job_id: str, actor: Literal["operator"], expected_spec_version: int | None = None
) -> None:
    _gate(
        store,
        job_id,
        actor,
        expected_spec_version,
        JobState.sample_ready,
        JobState.sample_approved,
        "sample_approved_at",
        "sample approved",
    )


def start_full_run(
    store: Store, job_id: str, actor: Literal["operator"], expected_spec_version: int | None = None
) -> None:
    _gate(
        store,
        job_id,
        actor,
        expected_spec_version,
        JobState.sample_approved,
        JobState.full_running,
        "full_started_at",
        "full run started",
    )


def edit_spec(store: Store, job_id: str, spec_version: int) -> None:
    current, _, resume_state, _ = _job(store, job_id)
    origin = resume_state if current in {JobState.paused, JobState.stalled} else current
    if current in {
        JobState.full_running,
        JobState.quote_check,
        JobState.building_output,
        JobState.verifying,
        JobState.done,
        JobState.done_with_issues,
    } or origin in {
        JobState.full_running,
        JobState.quote_check,
        JobState.building_output,
        JobState.verifying,
    }:
        raise TransitionError("spec cannot be edited after the full run starts")
    with store.transaction() as cur:
        cur.execute(
            "UPDATE jobs SET state='plan_proposed', spec_version=?, plan_approved_at=NULL, sample_approved_at=NULL, pause_reason=NULL, resume_state=NULL, updated_at=? WHERE id=?",
            (spec_version, _timestamp(), job_id),
        )
        cur.execute(
            "UPDATE attempts SET ended_at=?, outcome='superseded_by_spec', counted=0 WHERE job_id=? AND ended_at IS NULL",
            (_timestamp(), job_id),
        )
        cur.execute(
            "UPDATE leads SET status='queued', reason=NULL WHERE job_id=? AND status IN ('in_flight','queued','done','masked') AND in_sample=1",
            (job_id,),
        )
        cur.execute(
            "UPDATE leads SET status='queued', reason=NULL WHERE job_id=? AND status='held_back' "
            "AND reason='every call exceeds the per-lead cap'",
            (job_id,),
        )
        cur.execute("DELETE FROM results WHERE job_id=?", (job_id,))
        _event(cur, job_id, current, JobState.plan_proposed, "spec edited")
    store.notify_transition(job_id, current.value, JobState.plan_proposed.value, "spec edited")


def pause(store: Store, job_id: str, reason: PauseReason, resume_at: int | None = None) -> None:
    current, _, remembered, _ = _job(store, job_id)
    if current not in {
        JobState.pulling,
        JobState.masking_sample,
        JobState.sample_running,
        JobState.full_running,
        JobState.stalled,
        JobState.quote_check,
        JobState.building_output,
        JobState.verifying,
    }:
        raise TransitionError("job is not running")
    with store.transaction() as cur:
        resume_target = remembered if current == JobState.stalled and remembered is not None else current
        changed = cur.execute(
            "UPDATE jobs SET state='paused', pause_reason=?, resume_state=?, resume_at=?, updated_at=? WHERE id=? AND state=?",
            (reason, resume_target, resume_at, _timestamp(), job_id, current),
        )
        if changed.rowcount != 1:
            raise TransitionError("job changed before pause")
        _event(cur, job_id, current, JobState.paused, reason)
    store.notify_transition(job_id, current.value, JobState.paused.value, reason.value)


def resume(store: Store, job_id: str) -> None:
    current, _, target, _ = _job(store, job_id)
    if current != JobState.paused or target is None:
        raise TransitionError("job is not paused")
    with store.transaction() as cur:
        changed = cur.execute(
            "UPDATE jobs SET state=?, pause_reason=NULL, resume_state=NULL, resume_at=NULL, updated_at=? WHERE id=? AND state='paused' AND resume_state=?",
            (target, _timestamp(), job_id, target),
        )
        if changed.rowcount != 1:
            raise TransitionError("job changed before resume")
        _event(cur, job_id, JobState.paused, target, "resumed")
    store.notify_transition(job_id, JobState.paused.value, target.value, "resumed")


def replace_pause(store: Store, job_id: str, reason: PauseReason, resume_at: int | None = None) -> None:
    current, _, _, _ = _job(store, job_id)
    if current != JobState.paused:
        raise TransitionError("job is not paused")
    with store.transaction() as cur:
        changed = cur.execute(
            "UPDATE jobs SET pause_reason=?, resume_at=?, updated_at=? WHERE id=? AND state='paused'",
            (reason, resume_at, _timestamp(), job_id),
        )
        if changed.rowcount != 1:
            raise TransitionError("job changed before pause replacement")
        cur.execute(
            "INSERT INTO job_events(job_id, ts, kind, data_json) VALUES (?, ?, 'pause', ?)",
            (job_id, _timestamp(), json.dumps({"reason": reason, "resume_at": resume_at, "replaced": True})),
        )
