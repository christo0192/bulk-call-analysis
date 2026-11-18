from __future__ import annotations

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
    capped: int
    not_found: int
    remaining: int
    checklist: tuple[ChecklistItem, ...]


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
    row = store.rows("SELECT state FROM jobs WHERE id=?", (job_id,))
    state = str(row[0][0]) if row else ""
    ordered = (
        "plan_approved",
        "pulling",
        "masking_sample",
        "sample_running",
        "sample_approved",
        "full_running",
        "full_running",
        "quote_check",
        "building_output",
        "verifying",
        "done",
    )
    result: list[ChecklistItem] = []
    state_rank = {value: index for index, value in enumerate(ordered)}
    rank = state_rank.get(state, -1)
    for index, name in enumerate(_CHECKLIST):
        if name == "leads selected":
            status: ChecklistStatus = "done" if statuses.total() else "pending"
        elif name == "transcripts pulled":
            status = (
                "done"
                if statuses[LeadStatus.pulled]
                + statuses[LeadStatus.queued]
                + statuses[LeadStatus.done]
                + statuses[LeadStatus.in_flight]
                > 0
                else "pending"
            )
        elif name == "sample masked":
            status = "done" if state_rank.get(state, -1) >= state_rank["sample_running"] else "pending"
        elif name == "sample approved":
            status = "done" if rank >= state_rank["sample_approved"] else "pending"
        elif name == "masking n/N":
            status = (
                "active"
                if state == "masking_sample"
                else ("done" if rank > state_rank["masking_sample"] else "pending")
            )
        elif name == "full run n/N":
            status = (
                "active"
                if state == "full_running"
                else ("done" if rank > state_rank["full_running"] else "pending")
            )
        elif name == "sample run":
            status = (
                "active"
                if state == "sample_running"
                else ("done" if rank > state_rank["sample_running"] else "pending")
            )
        elif name == "quote check and repair":
            status = (
                "active"
                if state == "quote_check"
                else ("done" if rank > state_rank["quote_check"] else "pending")
            )
        elif name == "Excel built":
            status = (
                "active"
                if state == "building_output"
                else ("done" if rank > state_rank["building_output"] else "pending")
            )
        elif name == "verified":
            status = (
                "active"
                if state == "verifying"
                else ("done" if state in {"done", "done_with_issues"} else "pending")
            )
        elif name == "summary written":
            status = "done" if state in {"done", "done_with_issues"} else "pending"
        else:
            status = "done" if rank >= index else ("active" if rank == index - 1 else "pending")
        result.append(ChecklistItem(name, status))
    return tuple(result)


def counts(store: Store, job_id: str) -> Counts:
    rows = store.rows("SELECT status, reason, calls_dropped_cap FROM leads WHERE job_id=?", (job_id,))
    statuses = Counter(str(row[0]) for row in rows)
    failed = Counter(str(row[1] or "unknown") for row in rows if str(row[0]) == LeadStatus.failed)
    done = statuses[LeadStatus.done]
    in_flight = statuses[LeadStatus.in_flight]
    retrying = int(
        store.rows(
            "SELECT count(*) FROM leads l WHERE l.job_id=? AND l.status='queued' "
            "AND EXISTS (SELECT 1 FROM attempts a WHERE a.job_id=l.job_id "
            "AND a.lead_id=l.lead_id AND a.counted=1)",
            (job_id,),
        )[0][0]
    )
    terminal = (
        done
        + sum(failed.values())
        + statuses[LeadStatus.no_transcript]
        + statuses[LeadStatus.not_found]
        + statuses[LeadStatus.held_back]
        + statuses[LeadStatus.too_short]
    )
    remaining = max(0, len(rows) - terminal)
    return Counts(
        done,
        in_flight,
        retrying,
        dict(failed),
        statuses[LeadStatus.held_back],
        statuses[LeadStatus.no_transcript],
        sum(1 for row in rows if int(row[2]) > 0),
        statuses[LeadStatus.not_found],
        remaining,
        _checklist(store, job_id, statuses),
    )
