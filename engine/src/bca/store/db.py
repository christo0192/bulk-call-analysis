from __future__ import annotations

# ruff: noqa: E501
import json
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

LATEST_SCHEMA_VERSION = 5


def _now() -> str:
    return datetime.now(UTC).isoformat()


class LeadStatus(StrEnum):
    pending = "pending"
    no_transcript = "no_transcript"
    transcription_failed = "transcription_failed"
    too_short = "too_short"
    not_found = "not_found"
    pulled = "pulled"
    held_back = "held_back"
    masked = "masked"
    queued = "queued"
    in_flight = "in_flight"
    done = "done"
    failed = "failed"
    not_started = "not_started"


class ClaimError(RuntimeError):
    """Raised when a lead is not available for an attempt."""


class Store:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.lock = threading.RLock()

    def close(self) -> None:
        with self.lock:
            self.connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Cursor]:
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield self.connection.cursor()
            except BaseException:
                self.connection.rollback()
                raise
            else:
                self.connection.commit()

    def create_project(self, project_id: str, title: str) -> None:
        with self.transaction() as cur:
            cur.execute("INSERT INTO projects VALUES (?, ?, ?)", (project_id, title, _now()))

    def create_job(self, job_id: str, project_id: str, spec_version: int, state: str = "draft") -> None:
        now = _now()
        with self.transaction() as cur:
            cur.execute(
                "INSERT INTO jobs(id, project_id, state, spec_version, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (job_id, project_id, state, spec_version, now, now),
            )

    def add_lead(
        self, job_id: str, lead_id: str, status: LeadStatus | str = LeadStatus.pending, **counts: int
    ) -> None:
        allowed = {
            "reason",
            "in_sample",
            "calls_pulled",
            "calls_sent",
            "calls_dropped_cap",
            "calls_dropped_unusable",
            "chars",
        }
        if set(counts) - allowed:
            raise ValueError("unknown lead count")
        cols = ["job_id", "lead_id", "status", *counts]
        with self.transaction() as cur:
            cur.execute(
                f"INSERT INTO leads({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                (job_id, lead_id, str(status), *counts.values()),
            )

    def append_event(self, job_id: str, kind: str, data: dict[str, object]) -> None:
        with self.transaction() as cur:
            cur.execute(
                "INSERT INTO job_events(job_id, ts, kind, data_json) VALUES (?, ?, ?, ?)",
                (job_id, _now(), kind, json.dumps(data)),
            )

    def start_attempt(
        self,
        job_id: str,
        lead_id: str,
        model: str,
        counted: bool = True,
        claimed: bool = False,
        spec_version: int | None = None,
    ) -> str:
        attempt_id, now = str(uuid.uuid4()), _now()
        with self.transaction() as cur:
            if claimed:
                available = cur.execute(
                    "SELECT status FROM leads WHERE job_id=? AND lead_id=? AND status='in_flight'",
                    (job_id, lead_id),
                ).fetchone()
                if available is None:
                    raise ClaimError(f"lead {lead_id!r} is not in flight")
            else:
                updated = cur.execute(
                    "UPDATE leads SET status='in_flight' WHERE job_id=? AND lead_id=? AND status='queued'",
                    (job_id, lead_id),
                )
                if updated.rowcount != 1:
                    raise ClaimError(f"lead {lead_id!r} is not queued")
            spec = cur.execute("SELECT spec_version FROM jobs WHERE id=?", (job_id,)).fetchone()
            if spec is None:
                raise ClaimError(f"job {job_id!r} not found")
            if spec_version is not None and int(spec[0]) != spec_version:
                raise ClaimError("attempt spec version is stale")
            cur.execute(
                "UPDATE attempts SET ended_at=?, outcome='interrupted', counted=0 "
                "WHERE job_id=? AND lead_id=? AND ended_at IS NULL",
                (now, job_id, lead_id),
            )
            cur.execute(
                "INSERT INTO attempts(attempt_id, job_id, lead_id, started_at, counted, model_requested, spec_version) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (attempt_id, job_id, lead_id, now, int(counted), model, spec[0]),
            )
        return attempt_id

    def claim_queued(
        self,
        job_id: str,
        lead_ids: list[str],
        sample: bool,
        bundle_metadata: dict[str, tuple[int, int, int]] | None = None,
    ) -> set[str]:
        claimed: set[str] = set()
        with self.transaction() as cur:
            for lead_id in lead_ids:
                updated = cur.execute(
                    "UPDATE leads SET status='in_flight' WHERE job_id=? AND lead_id=? "
                    "AND status='queued' AND in_sample=?",
                    (job_id, lead_id, int(sample)),
                )
                if updated.rowcount == 1:
                    claimed.add(lead_id)
                    if bundle_metadata is not None and lead_id in bundle_metadata:
                        calls_sent, calls_dropped_cap, chars = bundle_metadata[lead_id]
                        cur.execute(
                            "UPDATE leads SET calls_sent=?, calls_dropped_cap=?, chars=? "
                            "WHERE job_id=? AND lead_id=?",
                            (calls_sent, calls_dropped_cap, chars, job_id, lead_id),
                        )
        return claimed

    def release_claim(self, job_id: str, lead_id: str) -> bool:
        """Return an unstarted claim to the queue without changing its reason."""
        with self.transaction() as cur:
            updated = cur.execute(
                "UPDATE leads SET status='queued' WHERE job_id=? AND lead_id=? "
                "AND status='in_flight' AND NOT EXISTS ("
                "SELECT 1 FROM attempts WHERE attempts.job_id=leads.job_id "
                "AND attempts.lead_id=leads.lead_id AND attempts.ended_at IS NULL)",
                (job_id, lead_id),
            )
            return updated.rowcount == 1

    def record_result(
        self,
        job_id: str,
        lead_id: str,
        attempt_id: str,
        answer: dict[str, object],
        quotes_ok: bool,
        flags: list[str] | dict[str, object],
        spec_version: int | None = None,
        model_used: str | None = None,
        usage: dict[str, object] | None = None,
        clear_repair_pending: bool = False,
    ) -> bool:
        with self.transaction() as cur:
            job = cur.execute("SELECT spec_version FROM jobs WHERE id=?", (job_id,)).fetchone()
            attempt = cur.execute(
                "SELECT spec_version FROM attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            if (
                job is None
                or attempt is None
                or attempt[0] != job[0]
                or (spec_version is not None and spec_version != attempt[0])
            ):
                self._stale(cur, job_id, lead_id, attempt_id)
                return False
            status = cur.execute(
                "SELECT status FROM leads WHERE job_id=? AND lead_id=?", (job_id, lead_id)
            ).fetchone()
            if status is None or status[0] != LeadStatus.in_flight:
                self._stale(cur, job_id, lead_id, attempt_id)
                return False
            updated = cur.execute(
                "UPDATE attempts SET ended_at=?, outcome='ok' WHERE attempt_id=? AND job_id=? AND lead_id=? AND ended_at IS NULL",
                (_now(), attempt_id, job_id, lead_id),
            )
            if updated.rowcount != 1:
                self._stale(cur, job_id, lead_id, attempt_id)
                return False
            cur.execute(
                "INSERT OR REPLACE INTO results VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    lead_id,
                    attempt_id,
                    json.dumps(answer),
                    int(quotes_ok),
                    json.dumps(flags),
                    _now(),
                    attempt[0],
                ),
            )
            if model_used is not None or usage:
                cur.execute(
                    "UPDATE attempts SET model_used=COALESCE(?, model_used), input_tokens=?, output_tokens=? WHERE attempt_id=?",
                    (
                        model_used,
                        _usage_value(usage, "inputTokens", "input_tokens"),
                        _usage_value(usage, "outputTokens", "output_tokens"),
                        attempt_id,
                    ),
                )
            if clear_repair_pending:
                cur.execute(
                    "UPDATE leads SET status='done', reason=NULL WHERE job_id=? AND lead_id=? "
                    "AND status='in_flight'",
                    (job_id, lead_id),
                )
            else:
                cur.execute(
                    "UPDATE leads SET status='done' WHERE job_id=? AND lead_id=? AND status='in_flight'",
                    (job_id, lead_id),
                )
            return True

    def record_reroute(self, attempt_id: str, from_model: str) -> None:
        with self.transaction() as cur:
            cur.execute(
                "UPDATE attempts SET rerouted_from=? WHERE attempt_id=? AND ended_at IS NULL",
                (from_model, attempt_id),
            )

    def record_model_used(self, attempt_id: str, model: str) -> None:
        with self.transaction() as cur:
            cur.execute(
                "UPDATE attempts SET model_used=? WHERE attempt_id=? AND ended_at IS NULL",
                (model, attempt_id),
            )

    def count_uncounted_failures(self, job_id: str, lead_id: str) -> int:
        rows = self.rows(
            "SELECT COUNT(*) FROM attempts WHERE job_id=? AND lead_id=? AND outcome='failed' "
            "AND counted=0 AND COALESCE(error_code, '') NOT IN "
            "('quota_interrupt', 'interrupted', 'appserver_crash', 'appserver_restart')",
            (job_id, lead_id),
        )
        return int(rows[0][0]) if rows else 0

    def count_appserver_crashes(self, job_id: str, lead_id: str, spec_version: int | None = None) -> int:
        spec_clause = " AND spec_version=?" if spec_version is not None else ""
        params: tuple[object, ...] = (job_id, lead_id) + ((spec_version,) if spec_version is not None else ())
        rows = self.rows(
            "SELECT COUNT(*) FROM attempts WHERE job_id=? AND lead_id=? "
            "AND outcome='failed' AND error_code='appserver_crash'" + spec_clause,
            params,
        )
        return int(rows[0][0]) if rows else 0

    @staticmethod
    def _stale(cur: sqlite3.Cursor, job_id: str, lead_id: str, attempt_id: str) -> None:
        cur.execute(
            "INSERT INTO job_events(job_id, ts, kind, data_json) VALUES (?, ?, 'stale_result', ?)",
            (job_id, _now(), json.dumps({"lead_id": lead_id, "attempt_id": attempt_id})),
        )

    def fail_attempt(
        self,
        attempt_id: str,
        error_code: str,
        counted: bool,
        max_attempts: int = 2,
        clear_repair_pending: bool = False,
    ) -> None:
        with self.transaction() as cur:
            row = cur.execute(
                "SELECT job_id, lead_id FROM attempts WHERE attempt_id=? AND ended_at IS NULL", (attempt_id,)
            ).fetchone()
            if row is None:
                raise ValueError("attempt is not open")
            job_id, lead_id = row
            cur.execute(
                "UPDATE attempts SET ended_at=?, outcome='failed', error_code=?, counted=? WHERE attempt_id=? AND ended_at IS NULL",
                (_now(), error_code, int(counted), attempt_id),
            )
            count = cur.execute(
                "SELECT count(*) FROM attempts WHERE job_id=? AND lead_id=? AND outcome='failed' AND counted=1",
                (job_id, lead_id),
            ).fetchone()[0]
            status = LeadStatus.failed if counted and count >= max_attempts else LeadStatus.queued
            cur.execute(
                "UPDATE leads SET status=?, reason=? WHERE job_id=? AND lead_id=?",
                (status, error_code if status == LeadStatus.failed else None, job_id, lead_id),
            )
            if clear_repair_pending:
                cur.execute(
                    "UPDATE leads SET reason=NULL WHERE job_id=? AND lead_id=?",
                    (job_id, lead_id),
                )

    def requeue_in_flight(self, job_id: str, reason: str, outcome: str = "requeued") -> int:
        with self.transaction() as cur:
            rows = cur.execute(
                "SELECT lead_id, attempt_id, "
                "(SELECT reason FROM leads WHERE leads.job_id=attempts.job_id "
                "AND leads.lead_id=attempts.lead_id) "
                "FROM attempts WHERE job_id=? AND ended_at IS NULL",
                (job_id,),
            ).fetchall()
            for lead_id, attempt_id, lead_reason in rows:
                reason_value = "quote_repair_pending" if lead_reason == "quote_repair_pending" else reason
                cur.execute(
                    "UPDATE attempts SET ended_at=?, outcome=?, counted=0, error_code=? WHERE attempt_id=?",
                    (_now(), outcome, reason_value, attempt_id),
                )
                cur.execute(
                    "UPDATE leads SET status='queued', reason=? WHERE job_id=? AND lead_id=?",
                    (reason_value, job_id, lead_id),
                )
            # Selection claims a batch before callbacks create attempts. If a
            # pause interrupts that batch, return those unstarted claims too,
            # keeping a pending quote repair so the resume sends the repair prompt.
            cur.execute(
                "UPDATE leads SET status='queued', "
                "reason=CASE WHEN reason='quote_repair_pending' THEN reason ELSE ? END "
                "WHERE job_id=? AND status='in_flight' "
                "AND NOT EXISTS (SELECT 1 FROM attempts WHERE attempts.job_id=leads.job_id "
                "AND attempts.lead_id=leads.lead_id AND attempts.ended_at IS NULL)",
                (reason, job_id),
            )
            return len(rows) + cur.rowcount

    def next_queued(self, job_id: str, limit: int, sample_only: bool) -> list[str]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT lead_id FROM leads WHERE job_id=? AND status='queued' AND (?=0 OR in_sample=1) ORDER BY lead_id LIMIT ?",
                (job_id, int(sample_only), limit),
            ).fetchall()
            return [str(row[0]) for row in rows]

    def set_lead_status(
        self, job_id: str, lead_id: str, status: LeadStatus | str, reason: str | None = None
    ) -> None:
        with self.transaction() as cur:
            cur.execute(
                "UPDATE leads SET status=?, reason=? WHERE job_id=? AND lead_id=?",
                (str(status), reason, job_id, lead_id),
            )

    def rows(self, query: str, args: tuple[object, ...] = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.connection.execute(query, args).fetchall()


def _run_migration(connection: sqlite3.Connection, statements: list[str], version: int) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        for statement in statements:
            connection.execute(statement)
        connection.execute(f"PRAGMA user_version={version}")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def open_store(path: Path) -> Store:
    connection = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version < 1:
        _run_migration(
            connection,
            [
                "CREATE TABLE projects(id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL)",
                "CREATE TABLE jobs(id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), state TEXT NOT NULL, pause_reason TEXT, spec_version INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, plan_approved_at TEXT, sample_approved_at TEXT, full_started_at TEXT, finished_at TEXT)",
                "CREATE TABLE job_events(id INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), ts TEXT NOT NULL, kind TEXT NOT NULL, data_json TEXT NOT NULL)",
                "CREATE TABLE leads(job_id TEXT NOT NULL REFERENCES jobs(id), lead_id TEXT NOT NULL, status TEXT NOT NULL, reason TEXT, in_sample INTEGER NOT NULL DEFAULT 0, calls_pulled INTEGER NOT NULL DEFAULT 0, calls_sent INTEGER NOT NULL DEFAULT 0, calls_dropped_cap INTEGER NOT NULL DEFAULT 0, calls_dropped_unusable INTEGER NOT NULL DEFAULT 0, chars INTEGER NOT NULL DEFAULT 0)",
                "CREATE INDEX leads_job_lead ON leads(job_id, lead_id)",
                "CREATE TABLE attempts(attempt_id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), lead_id TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT, outcome TEXT, error_code TEXT, counted INTEGER NOT NULL DEFAULT 1, model_requested TEXT NOT NULL, model_used TEXT, rerouted_from TEXT, input_tokens INTEGER, output_tokens INTEGER)",
                "CREATE TABLE results(job_id TEXT NOT NULL, lead_id TEXT NOT NULL, attempt_id TEXT NOT NULL, answer_json TEXT NOT NULL, quotes_ok INTEGER NOT NULL, flags_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(job_id, lead_id))",
                "CREATE TABLE id_intake(job_id TEXT PRIMARY KEY REFERENCES jobs(id), raw_count INTEGER NOT NULL, distinct_count INTEGER NOT NULL, duplicates INTEGER NOT NULL, malformed INTEGER NOT NULL)",
            ],
            1,
        )
        version = 1
    if version < 2:
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute("DROP TABLE IF EXISTS leads_new")
            duplicate = connection.execute(
                "SELECT job_id, lead_id FROM leads GROUP BY job_id, lead_id HAVING count(*) > 1 LIMIT 1"
            ).fetchone()
            if duplicate is not None:
                raise RuntimeError(
                    f"duplicate (job_id, lead_id) rows in leads: {duplicate[0]!r}, {duplicate[1]!r}"
                )
            for statement in [
                "CREATE TABLE leads_new(job_id TEXT NOT NULL REFERENCES jobs(id), lead_id TEXT NOT NULL, status TEXT NOT NULL, reason TEXT, in_sample INTEGER NOT NULL DEFAULT 0, calls_pulled INTEGER NOT NULL DEFAULT 0, calls_sent INTEGER NOT NULL DEFAULT 0, calls_dropped_cap INTEGER NOT NULL DEFAULT 0, chars INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(job_id, lead_id))",
                "INSERT INTO leads_new SELECT job_id, lead_id, status, reason, in_sample, calls_pulled, calls_sent, calls_dropped_cap, chars FROM leads",
                "DROP TABLE leads",
                "ALTER TABLE leads_new RENAME TO leads",
                "CREATE INDEX leads_job_lead ON leads(job_id, lead_id)",
            ]:
                connection.execute(statement)
            connection.execute("PRAGMA user_version=2")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        version = 2
    if version < 3:
        _run_migration(
            connection,
            [
                "ALTER TABLE jobs ADD COLUMN resume_state TEXT",
                "ALTER TABLE attempts ADD COLUMN spec_version INTEGER NOT NULL DEFAULT 1",
                "ALTER TABLE results ADD COLUMN spec_version INTEGER NOT NULL DEFAULT 1",
                "CREATE TABLE attempts_new(attempt_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, lead_id TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT, outcome TEXT, error_code TEXT, counted INTEGER NOT NULL DEFAULT 1, model_requested TEXT NOT NULL, model_used TEXT, rerouted_from TEXT, input_tokens INTEGER, output_tokens INTEGER, spec_version INTEGER NOT NULL, FOREIGN KEY(job_id, lead_id) REFERENCES leads(job_id, lead_id))",
                "INSERT INTO attempts_new SELECT attempt_id, job_id, lead_id, started_at, ended_at, outcome, error_code, counted, model_requested, model_used, rerouted_from, input_tokens, output_tokens, spec_version FROM attempts",
                "DROP TABLE attempts",
                "ALTER TABLE attempts_new RENAME TO attempts",
                "CREATE TABLE results_new(job_id TEXT NOT NULL, lead_id TEXT NOT NULL, attempt_id TEXT NOT NULL, answer_json TEXT NOT NULL, quotes_ok INTEGER NOT NULL, flags_json TEXT NOT NULL, created_at TEXT NOT NULL, spec_version INTEGER NOT NULL, PRIMARY KEY(job_id, lead_id), FOREIGN KEY(job_id, lead_id) REFERENCES leads(job_id, lead_id))",
                "INSERT INTO results_new SELECT job_id, lead_id, attempt_id, answer_json, quotes_ok, flags_json, created_at, spec_version FROM results",
                "DROP TABLE results",
                "ALTER TABLE results_new RENAME TO results",
            ],
            3,
        )
        version = 3
    if version < 4:
        _run_migration(
            connection,
            [
                "ALTER TABLE jobs ADD COLUMN quota_baseline TEXT",
                "ALTER TABLE jobs ADD COLUMN resume_at INTEGER",
            ],
            4,
        )
        version = 4
    if version < LATEST_SCHEMA_VERSION:
        _run_migration(
            connection,
            [
                "ALTER TABLE leads ADD COLUMN calls_dropped_unusable INTEGER NOT NULL DEFAULT 0",
            ],
            5,
        )
    return Store(connection)


def _usage_value(usage: dict[str, object] | None, *names: str) -> int | None:
    if not usage:
        return None
    for name in names:
        value = usage.get(name)
        if isinstance(value, int):
            return value
    return None
