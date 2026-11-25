from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from typing import Literal

from bca.store import LeadStatus, Store

ChecklistStatus = Literal["pending", "active", "done", "failed"]


@dataclass(frozen=True)
class ChecklistItem:
    name: str
    status: ChecklistStatus


@dataclass(frozen=True)
class Counts:
    done: int
    in_flight: int
    retrying: int
    failed: dict[str, int]
    held_back: int
    no_transcript: int
    transcription_failed: int
    too_short: int
    capped: int
    not_found: int
    not_started: int
    remaining: int
    checklist: tuple[ChecklistItem, ...]
    total: int


def progress_sample_scope(store: Store, job_id: str) -> bool | None:
    """Return sample-only progress during the sample phase, or all leads."""
    row = store.rows("SELECT state, resume_state FROM jobs WHERE id=?", (job_id,))
    if not row:
        return None
    state = str(row[0][0])
    if state in {"paused", "stalled"} and row[0][1]:
        state = str(row[0][1])
    elif state in {"failed", "cancelled"}:
        for event_row in store.rows(
            "SELECT data_json FROM job_events WHERE job_id=? AND kind='state' ORDER BY id DESC",
            (job_id,),
        ):
            try:
                event = json.loads(str(event_row[0]))
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and str(event.get("to")) not in {"failed", "cancelled"}:
                state = str(event.get("to", state))
                break
    return True if state in {"pulling", "masking_sample", "sample_running", "sample_ready"} else None


_CHECKLIST = (
    "plan approved",
    "leads selected",
    "transcripts pulled",
    "sample masked",
    "sample run",
    "sample approved",
    "masking n/N",
    "full run n/N",
    "quote check and repair",
    "Excel built",
    "verified",
    "summary written",
)


def _checklist(store: Store, job_id: str, statuses: Counter[str]) -> tuple[ChecklistItem, ...]:
    row = store.rows("SELECT state, resume_state FROM jobs WHERE id=?", (job_id,))
    state = str(row[0][0]) if row else ""
    if state in {"paused", "stalled"} and row and row[0][1]:
        state = str(row[0][1])
    phase = {
        "plan_approved": "plan approved",
        "pulling": "transcripts pulled",
        "masking_sample": "sample masked",
        "sample_running": "sample run",
        "sample_ready": "sample approved",
        "sample_approved": "masking n/N",
        "full_running": "full run n/N",
        "quote_check": "quote check and repair",
        "building_output": "Excel built",
        "verifying": "verified",
    }
    if state in {"done", "done_with_issues"}:
        return tuple(ChecklistItem(name, "done") for name in _CHECKLIST)
    active = phase.get(state)
    active_index = _CHECKLIST.index(active) if active else -1
    result: list[ChecklistItem] = []
    for index, name in enumerate(_CHECKLIST):
        if name == "leads selected":
            status: ChecklistStatus = "done" if statuses.total() else "pending"
        elif name == "transcripts pulled":
            pending = statuses[LeadStatus.pending] + statuses[LeadStatus.not_started]
            status = (
                "active"
                if state == "pulling" and pending
                else ("done" if pending == 0 and statuses.total() else "pending")
            )
        elif state == "failed" and index == 0:
            status = "failed"
        elif active_index >= 0 and index < active_index:
            status = "done"
        elif index == active_index:
            status = "active"
        else:
            status = "pending"
        result.append(ChecklistItem(name, status))
    return tuple(result)


def counts(store: Store, job_id: str, sample: bool | None = None) -> Counts:
    with store.lock:
        clause = " AND in_sample=?" if sample is not None else ""
        args: tuple[object, ...] = (job_id, int(sample)) if sample is not None else (job_id,)
        rows = store.rows(f"SELECT status, reason, calls_dropped_cap FROM leads WHERE job_id=?{clause}", args)
        statuses = Counter(str(row[0]) for row in rows)
        failed = Counter(str(row[1] or "unknown") for row in rows if str(row[0]) == LeadStatus.failed)
        retrying = int(
            store.rows(
                f"SELECT count(*) FROM leads l WHERE l.job_id=? AND l.status='queued' {clause} "
                "AND EXISTS (SELECT 1 FROM attempts a WHERE a.job_id=l.job_id "
                "AND a.lead_id=l.lead_id AND a.counted=1 AND a.outcome='failed')",
                args,
            )[0][0]
        )
        unusable = sum(
            statuses[status]
            for status in (
                LeadStatus.no_transcript,
                LeadStatus.transcription_failed,
                LeadStatus.too_short,
                LeadStatus.not_found,
            )
        )
        total = len(rows) - unusable
        remaining = (
            int(
                store.rows(
                    "SELECT count(*) FROM leads l WHERE l.job_id=? "
                    "AND l.status IN ('pending', 'pulled', 'masked', 'queued', 'not_started') "
                    f"{clause}",
                    args,
                )[0][0]
            )
            - retrying
        )
        remaining = max(0, remaining)
        return Counts(
            statuses[LeadStatus.done],
            statuses[LeadStatus.in_flight],
            retrying,
            dict(failed),
            statuses[LeadStatus.held_back],
            statuses[LeadStatus.no_transcript],
            statuses[LeadStatus.transcription_failed],
            statuses[LeadStatus.too_short],
            sum(int(row[2]) > 0 for row in rows),
            statuses[LeadStatus.not_found],
            statuses[LeadStatus.not_started],
            remaining,
            _checklist(store, job_id, statuses),
            total,
        )
