import sqlite3
import threading
from pathlib import Path

import pytest

from bca.store import ClaimError, LeadStatus, Store, open_store
from bca.store.db import LATEST_SCHEMA_VERSION


def prepared_store(path: Path | str = ":memory:") -> Store:
    store = open_store(Path(path))
    store.create_project("p", "Project")
    store.create_job("j", "p", 1)
    return store


def test_migrations_are_applied_and_reopen_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "store.sqlite"
    store = prepared_store(path)
    assert store.rows("PRAGMA user_version")[0][0] == LATEST_SCHEMA_VERSION
    assert store.rows("PRAGMA journal_mode")[0][0] == "wal"
    store.close()
    reopened = open_store(path)
    assert reopened.rows("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    assert reopened.rows("PRAGMA user_version")[0][0] == LATEST_SCHEMA_VERSION
    reopened.close()


def test_version_3_migration_preserves_rows(tmp_path: Path) -> None:
    path = tmp_path / "version-3.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA user_version=3;
        CREATE TABLE projects(id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE jobs(id TEXT PRIMARY KEY, project_id TEXT NOT NULL, state TEXT NOT NULL,
            pause_reason TEXT, spec_version INTEGER NOT NULL, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, plan_approved_at TEXT, sample_approved_at TEXT,
            full_started_at TEXT, finished_at TEXT, resume_state TEXT);
        CREATE TABLE leads(job_id TEXT NOT NULL, lead_id TEXT NOT NULL, status TEXT NOT NULL,
            reason TEXT, in_sample INTEGER NOT NULL DEFAULT 0, calls_pulled INTEGER NOT NULL DEFAULT 0,
            calls_sent INTEGER NOT NULL DEFAULT 0, calls_dropped_cap INTEGER NOT NULL DEFAULT 0,
            chars INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(job_id, lead_id));
        INSERT INTO projects VALUES ('p', 'Project', 'now');
        INSERT INTO jobs VALUES ('j', 'p', 'draft', NULL, 1, 'now', 'now', NULL, NULL, NULL, NULL, NULL);
        INSERT INTO leads(job_id, lead_id, status) VALUES ('j', 'lead-1', 'queued');
        """
    )
    connection.commit()
    connection.close()

    store = open_store(path)
    assert store.rows("PRAGMA user_version")[0][0] == LATEST_SCHEMA_VERSION
    assert store.rows("SELECT lead_id, status FROM leads")[0][:] == ("lead-1", "queued")
    assert store.rows("SELECT quota_baseline, resume_at FROM jobs")[0][:] == (None, None)
    store.close()


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
    with pytest.raises(ClaimError):
        store.start_attempt("j", "a", "model")
    store.fail_attempt(first, "temporary", counted=False)
    second = store.start_attempt("j", "a", "model")
    assert store.rows("SELECT outcome FROM attempts WHERE attempt_id=?", (first,))[0][0] == "failed"
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
