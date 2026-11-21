from pathlib import Path

from bca.monitor.progress import counts
from bca.store import (
    JobState,
    LeadStatus,
    approve_plan,
    approve_sample,
    open_store,
    reconcile,
    start_full_run,
    transition,
)


def test_counts_reconcile_synthetic_store() -> None:
    store = open_store(Path(":memory:"))
    store.create_project("project", "Project")
    store.create_job("job", "project", 1)
    store.add_lead("job", "done", LeadStatus.queued, calls_pulled=1, calls_sent=1)
    store.add_lead("job", "held", LeadStatus.held_back)
    store.add_lead("job", "missing", LeadStatus.no_transcript)
    store.add_lead("job", "queued", LeadStatus.queued, calls_pulled=2, calls_sent=1, calls_dropped_cap=1)
    store.add_lead("job", "running", LeadStatus.in_flight)
    attempt = store.start_attempt("job", "done", "model")
    assert store.record_result("job", "done", attempt, {}, True, [])
    with store.transaction() as cursor:
        cursor.execute("INSERT INTO id_intake VALUES (?, ?, ?, ?, ?)", ("job", 5, 5, 0, 0))
    result = counts(store, "job")
    assert result.done == 1
    assert result.held_back == 1 and result.no_transcript == 1 and result.capped == 1
    assert result.in_flight == 1 and result.remaining == 1
    assert len(result.checklist) == 12
    reconciliation = reconcile(store, "job")
    assert reconciliation.ok
    assert reconciliation.counts[LeadStatus.done.value] == result.done
    assert reconciliation.counts[LeadStatus.held_back.value] == result.held_back
    assert reconciliation.counts[LeadStatus.no_transcript.value] == result.no_transcript
    assert sum(reconciliation.counts.values()) == 5
    store.close()


def test_progress_excludes_unusable_leads_from_total_and_remaining() -> None:
    store = open_store(Path(":memory:"))
    store.create_project("project", "Project")
    store.create_job("job", "project", 1)
    store.add_lead("job", "done", LeadStatus.queued)
    attempt = store.start_attempt("job", "done", "model")
    assert store.record_result("job", "done", attempt, {}, True, [])
    for lead_id, status in (
        ("missing", LeadStatus.no_transcript),
        ("short", LeadStatus.too_short),
        ("failed", LeadStatus.transcription_failed),
    ):
        store.add_lead("job", lead_id, status)
    result = counts(store, "job")
    assert result.total == 1
    assert result.remaining == 0
    assert result.no_transcript == 1
    assert result.too_short == 1
    assert result.transcription_failed == 1
    store.close()


def test_checklist_statuses_for_mid_run_job() -> None:
    store = open_store(Path(":memory:"))
    store.create_project("project", "Project")
    store.create_job("job", "project", 1)
    store.add_lead("job", "lead", LeadStatus.queued)
    transition(store, "job", JobState.plan_proposed)
    approve_plan(store, "job", "operator")
    transition(store, "job", JobState.pulling)
    transition(store, "job", JobState.masking_sample)
    transition(store, "job", JobState.sample_running)
    transition(store, "job", JobState.sample_ready)
    approve_sample(store, "job", "operator")
    start_full_run(store, "job", "operator")

    checklist = {item.name: item.status for item in counts(store, "job").checklist}
    assert checklist["plan approved"] == "done"
    assert checklist["sample run"] == "done"
    assert checklist["full run n/N"] == "active"
    assert checklist["quote check and repair"] == "pending"
    assert checklist["summary written"] == "pending"
    store.close()


def test_counts_expose_each_terminal_status_and_retry_only_failed_attempts() -> None:
    store = open_store(Path(":memory:"))
    store.create_project("project", "Project")
    store.create_job("job", "project", 1, state="done_with_issues")
    for lead_id, status in {
        "done": LeadStatus.done,
        "failed": LeadStatus.failed,
        "transcription": LeadStatus.transcription_failed,
        "short": LeadStatus.too_short,
        "not_started": LeadStatus.not_started,
    }.items():
        store.add_lead("job", lead_id, status)
    result = counts(store, "job")
    assert result.done == 1
    assert result.transcription_failed == 1 and result.too_short == 1 and result.not_started == 1
    assert result.remaining == 1
    assert (
        sum(
            (
                result.done,
                sum(result.failed.values()),
                result.transcription_failed,
                result.too_short,
                result.not_started,
            )
        )
        == 5
    )
    assert all(item.status == "done" for item in result.checklist)
    store.close()


def test_retrying_excludes_a_lead_with_only_a_successful_attempt() -> None:
    store = open_store(Path(":memory:"))
    store.create_project("project", "Project")
    store.create_job("job", "project", 1)
    store.add_lead("job", "succeeded", LeadStatus.queued)
    attempt = store.start_attempt("job", "succeeded", "model")
    assert store.record_result("job", "succeeded", attempt, {}, True, [])
    store.connection.execute("UPDATE leads SET status='queued' WHERE lead_id='succeeded'")
    store.connection.commit()
    assert counts(store, "job").retrying == 0
    store.close()
