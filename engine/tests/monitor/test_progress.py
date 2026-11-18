from pathlib import Path

from bca.monitor.progress import counts
from bca.store import LeadStatus, open_store, reconcile


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
    assert result.in_flight == 1 and result.remaining == 2
    assert len(result.checklist) == 12
    assert reconcile(store, "job").ok
    store.close()
