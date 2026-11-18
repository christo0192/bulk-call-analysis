import sqlite3
import threading
from pathlib import Path

import pytest

from bca.store import LeadStatus, Store, open_store


def prepared_store(path: Path | str = ":memory:") -> Store:
    store = open_store(Path(path))
    store.create_project("p", "Project")
    store.create_job("j", "p", 1)
    return store


def test_migrations_are_applied_and_reopen_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "store.sqlite"
    store = prepared_store(path)
    assert store.rows("PRAGMA user_version")[0][0] == 2
    assert store.rows("PRAGMA journal_mode")[0][0] == "wal"
    store.close()
    reopened = open_store(path)
    assert reopened.rows("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    assert reopened.rows("PRAGMA user_version")[0][0] == 2
    reopened.close()


def test_duplicate_lead_is_rejected() -> None:
    store = prepared_store()
    store.add_lead("j", "lead")
    with pytest.raises(sqlite3.IntegrityError):
        store.add_lead("j", "lead")


def test_attempt_lifecycle_and_queue_order() -> None:
    store = prepared_store()
    store.add_lead("j", "b", LeadStatus.queued, in_sample=1)
    store.add_lead("j", "a", LeadStatus.queued, in_sample=0)
    store.add_lead("j", "c", LeadStatus.queued, in_sample=1)
    assert store.next_queued("j", 10, False) == ["a", "b", "c"]
    assert store.next_queued("j", 10, True) == ["b", "c"]
    first = store.start_attempt("j", "a", "model")
    second = store.start_attempt("j", "a", "model")
    assert store.rows("SELECT outcome FROM attempts WHERE attempt_id=?", (first,))[0][0] == "superseded"
    store.fail_attempt(second, "temporary", counted=False)
    assert store.rows("SELECT status FROM leads WHERE lead_id='a'")[0][0] == LeadStatus.queued
    third = store.start_attempt("j", "a", "model")
    store.fail_attempt(third, "fatal", counted=True, max_attempts=1)
    assert store.rows("SELECT status FROM leads WHERE lead_id='a'")[0][0] == LeadStatus.failed
    store.close()


def test_requeue_and_current_result() -> None:
    store = prepared_store()
    store.add_lead("j", "old", LeadStatus.queued)
    attempt = store.start_attempt("j", "old", "model")
    assert store.requeue_in_flight("j", "restart") == 1
    row = store.rows("SELECT outcome, counted FROM attempts WHERE attempt_id=?", (attempt,))[0]
    assert (row[0], row[1]) == ("requeued", 0)
    assert not store.record_result("j", "old", attempt, {}, True, [])
    assert store.rows("SELECT kind FROM job_events WHERE job_id='j'")[-1][0] == "stale_result"
    current = store.start_attempt("j", "old", "model")
    assert store.record_result("j", "old", current, {"x": 1}, True, [])
    assert store.rows("SELECT status FROM leads WHERE lead_id='old'")[0][0] == LeadStatus.done
    assert store.rows("SELECT outcome FROM attempts WHERE attempt_id=?", (current,))[0][0] == "ok"


def test_concurrent_results() -> None:
    store = prepared_store()
    for i in range(200):
        store.add_lead("j", f"lead-{i:03d}", LeadStatus.queued)
    errors: list[BaseException] = []

    def worker(offset: int) -> None:
        try:
            for i in range(offset, 200, 4):
                lead = f"lead-{i:03d}"
                attempt = store.start_attempt("j", lead, "model")
                assert store.record_result("j", lead, attempt, {}, True, [])
        except BaseException as exc:  # pragma: no cover - assertion is reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(offset,)) for offset in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert store.rows("SELECT count(*) FROM leads WHERE status='done'")[0][0] == 200
