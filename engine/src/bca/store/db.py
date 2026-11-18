from __future__ import annotations

# SQL migration statements are intentionally kept readable as complete statements.
# ruff: noqa: E501
import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path


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


class Store:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.lock = threading.RLock()

    def close(self) -> None:
        with self.lock:
            self.connection.close()

    def _transaction(self) -> sqlite3.Cursor:
        self.connection.execute("BEGIN IMMEDIATE")
        return self.connection.cursor()

    def create_project(self, project_id: str, title: str) -> None:
        with self.lock, self.connection:
            self.connection.execute("INSERT INTO projects VALUES (?, ?, ?)", (project_id, title, _now()))

    def create_job(self, job_id: str, project_id: str, spec_version: int, state: str = "draft") -> None:
        now = _now()
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT INTO jobs(id, project_id, state, spec_version, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (job_id, project_id, state, spec_version, now, now),
            )

    def add_lead(
        self, job_id: str, lead_id: str, status: LeadStatus | str = LeadStatus.pending, **counts: int
    ) -> None:
        fields = {"status": str(status), **counts}
        allowed = {"reason", "in_sample", "calls_pulled", "calls_sent", "calls_dropped_cap", "chars"}
        if set(counts) - allowed:
            raise ValueError("unknown lead count")
        cols = ["job_id", "lead_id", "status", *list(counts)]
        with self.lock, self.connection:
            self.connection.execute(
                f"INSERT INTO leads({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                (job_id, lead_id, fields["status"], *counts.values()),
            )

    def append_event(self, job_id: str, kind: str, data: dict[str, object]) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT INTO job_events(job_id, ts, kind, data_json) VALUES (?, ?, ?, ?)",
                (job_id, _now(), kind, json.dumps(data)),
            )

    def start_attempt(self, job_id: str, lead_id: str, model: str, counted: bool = True) -> str:
        attempt_id = str(uuid.uuid4())
        now = _now()
        with self.lock, self.connection:
            self.connection.execute(
                "UPDATE attempts SET ended_at=?, outcome='superseded' WHERE job_id=? "
                "AND lead_id=? AND ended_at IS NULL",
                (now, job_id, lead_id),
            )
            self.connection.execute(
                "INSERT INTO attempts VALUES (?, ?, ?, ?, NULL, NULL, NULL, ?, ?, NULL, NULL, NULL, NULL)",
                (attempt_id, job_id, lead_id, now, 1 if counted else 0, model),
            )
            self.connection.execute(
                "UPDATE leads SET status=? WHERE job_id=? AND lead_id=?",
                (LeadStatus.in_flight, job_id, lead_id),
            )
        return attempt_id

    def record_result(
        self,
        job_id: str,
        lead_id: str,
        attempt_id: str,
        answer: dict[str, object],
        quotes_ok: bool,
        flags: list[str] | dict[str, object],
    ) -> bool:
        with self.lock, self.connection:
            current = self.connection.execute(
                "SELECT attempt_id FROM attempts WHERE job_id=? AND lead_id=? "
                "AND ended_at IS NULL ORDER BY started_at DESC LIMIT 1",
                (job_id, lead_id),
            ).fetchone()
            if current is None or current[0] != attempt_id:
                self.connection.execute(
                    "INSERT INTO job_events(job_id, ts, kind, data_json) VALUES (?, ?, 'stale_result', ?)",
                    (job_id, _now(), json.dumps({"lead_id": lead_id, "attempt_id": attempt_id})),
                )
                return False
            self.connection.execute(
                "INSERT OR REPLACE INTO results VALUES (?, ?, ?, ?, ?, ?, ?)",
                (job_id, lead_id, attempt_id, json.dumps(answer), int(quotes_ok), json.dumps(flags), _now()),
            )
            self.connection.execute(
                "UPDATE attempts SET ended_at=?, outcome='ok' WHERE attempt_id=?", (_now(), attempt_id)
            )
            self.connection.execute(
                "UPDATE leads SET status='done' WHERE job_id=? AND lead_id=?", (job_id, lead_id)
            )
            return True

    def fail_attempt(self, attempt_id: str, error_code: str, counted: bool, max_attempts: int = 2) -> None:
        with self.lock, self.connection:
            row = self.connection.execute(
                "SELECT job_id, lead_id FROM attempts WHERE attempt_id=? AND ended_at IS NULL", (attempt_id,)
            ).fetchone()
            if row is None:
                raise ValueError("attempt is not open")
            job_id, lead_id = row
            self.connection.execute(
                "UPDATE attempts SET ended_at=?, outcome='failed', error_code=?, "
                "counted=? WHERE attempt_id=?",
                (_now(), error_code, int(counted), attempt_id),
            )
            count = self.connection.execute(
                "SELECT count(*) FROM attempts WHERE job_id=? AND lead_id=? AND counted=1", (job_id, lead_id)
            ).fetchone()[0]
            status = LeadStatus.failed if counted and count >= max_attempts else LeadStatus.queued
            self.connection.execute(
                "UPDATE leads SET status=?, reason=? WHERE job_id=? AND lead_id=?",
                (status, error_code if status == LeadStatus.failed else None, job_id, lead_id),
            )

    def requeue_in_flight(self, job_id: str, reason: str) -> int:
        with self.lock, self.connection:
            rows = self.connection.execute(
                "SELECT lead_id, attempt_id FROM attempts WHERE job_id=? AND ended_at IS NULL", (job_id,)
            ).fetchall()
            count = 0
            for lead_id, attempt_id in rows:
                self.connection.execute(
                    "UPDATE attempts SET ended_at=?, outcome='requeued', counted=0, "
                    "error_code=? WHERE attempt_id=?",
                    (_now(), reason, attempt_id),
                )
                self.connection.execute(
                    "UPDATE leads SET status='queued', reason=? WHERE job_id=? AND lead_id=?",
                    (reason, job_id, lead_id),
                )
                count += 1
            return count

    def next_queued(self, job_id: str, limit: int, sample_only: bool) -> list[str]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT lead_id FROM leads WHERE job_id=? AND status='queued' "
                "AND (?=0 OR in_sample=1) ORDER BY lead_id LIMIT ?",
                (job_id, int(sample_only), limit),
            ).fetchall()
            return [str(row[0]) for row in rows]

    def set_lead_status(
        self, job_id: str, lead_id: str, status: LeadStatus | str, reason: str | None = None
    ) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                "UPDATE leads SET status=?, reason=? WHERE job_id=? AND lead_id=?",
                (str(status), reason, job_id, lead_id),
            )

    def rows(self, query: str, args: tuple[object, ...] = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.connection.execute(query, args).fetchall()


def open_store(path: Path) -> Store:
    connection = sqlite3.connect(path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version < 1:
        connection.executescript("""
        CREATE TABLE projects(id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE jobs(id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), state TEXT NOT NULL, pause_reason TEXT, spec_version INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, plan_approved_at TEXT, sample_approved_at TEXT, full_started_at TEXT, finished_at TEXT); -- noqa: E501
        CREATE TABLE job_events(id INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), ts TEXT NOT NULL, kind TEXT NOT NULL, data_json TEXT NOT NULL); -- noqa: E501
        CREATE TABLE leads(job_id TEXT NOT NULL REFERENCES jobs(id), lead_id TEXT NOT NULL, status TEXT NOT NULL, reason TEXT, in_sample INTEGER NOT NULL DEFAULT 0, calls_pulled INTEGER NOT NULL DEFAULT 0, calls_sent INTEGER NOT NULL DEFAULT 0, calls_dropped_cap INTEGER NOT NULL DEFAULT 0, chars INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(job_id, lead_id)); -- noqa: E501
        CREATE TABLE attempts(attempt_id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), lead_id TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT, outcome TEXT, error_code TEXT, counted INTEGER NOT NULL DEFAULT 1, model_requested TEXT NOT NULL, model_used TEXT, rerouted_from TEXT, input_tokens INTEGER, output_tokens INTEGER); -- noqa: E501
        CREATE TABLE results(job_id TEXT NOT NULL, lead_id TEXT NOT NULL, attempt_id TEXT NOT NULL, answer_json TEXT NOT NULL, quotes_ok INTEGER NOT NULL, flags_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(job_id, lead_id)); -- noqa: E501
        CREATE TABLE id_intake(job_id TEXT PRIMARY KEY REFERENCES jobs(id), raw_count INTEGER NOT NULL, distinct_count INTEGER NOT NULL, duplicates INTEGER NOT NULL, malformed INTEGER NOT NULL); -- noqa: E501
        PRAGMA user_version=1;
        """)
    return Store(connection)
