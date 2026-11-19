from __future__ import annotations

# ruff: noqa: E501
from collections.abc import Sequence
from dataclasses import dataclass

from .db import LeadStatus, Store


def _as_int(value: object) -> int:
    if isinstance(value, int):
        return value
    raise TypeError(f"expected integer row value, got {type(value).__name__}")


@dataclass
class Reconciliation:
    distinct_ids_in: int
    counts: dict[str, int]
    duplicates_removed: int
    call_imbalances: list[str]
    problems: list[str]

    @property
    def ok(self) -> bool:
        return not self.problems

    def to_markdown(self) -> str:
        lines = ["| Status | Count |", "|---|---:|"]
        lines.extend(f"| {key} | {value} |" for key, value in sorted(self.counts.items()))
        lines.append(f"| distinct_ids_in | {self.distinct_ids_in} |")
        lines.append(f"| duplicates_removed | {self.duplicates_removed} |")
        if self.problems:
            lines.append("\nProblems:\n" + "\n".join(f"- {problem}" for problem in self.problems))
        return "\n".join(lines)


def reconcile_rows(
    lead_rows: Sequence[Sequence[object]],
    result_rows: Sequence[Sequence[object]],
    intake: Sequence[object] | None,
    *,
    final: bool = False,
) -> Reconciliation:
    counts = {status.value: 0 for status in LeadStatus}
    leads: dict[str, str] = {}
    problems: list[str] = []
    for row in lead_rows:
        lead_id, status = str(row[0]), str(row[1])
        if lead_id in leads:
            problems.append("duplicate lead rows")
        counts[status] = counts.get(status, 0) + 1
        leads[lead_id] = status
        if status not in {item.value for item in LeadStatus}:
            problems.append(f"unknown lead status {status} for {lead_id}")
        unusable = _as_int(row[5]) if len(row) > 5 else 0
        pulled, sent, capped = (_as_int(row[index]) for index in (2, 3, 4))
        mismatch = pulled != sent + capped + unusable if pulled else bool(sent + capped)
        if mismatch:
            problems.append(f"call identity mismatch for {lead_id}")
    distinct = _as_int(intake[1]) if intake else 0
    duplicates = _as_int(intake[2]) if intake else 0
    if intake and len(leads) != distinct:
        problems.append(f"lead identity mismatch: {distinct} distinct ids in, {len(leads)} leads stored")
    if intake and _as_int(intake[0]) != _as_int(intake[1]) + _as_int(intake[2]) + _as_int(intake[3]):
        problems.append("id intake counts do not reconcile")
    done = {lead for lead, status in leads.items() if status == LeadStatus.done}
    result_leads = {str(row[0]) for row in result_rows}
    if result_leads != done:
        problems.append("results do not equal done leads")
    result_attempts = {str(row[0]): str(row[1]) for row in result_rows}
    for lead, attempt in result_attempts.items():
        if attempt != result_attempts.get(lead):
            problems.append(f"result attempt mismatch for {lead}")
    if final:
        pending = {
            LeadStatus.pending.value,
            LeadStatus.pulled.value,
            LeadStatus.masked.value,
            LeadStatus.queued.value,
            LeadStatus.in_flight.value,
        }
        problems.extend(f"pending lead {lead}" for lead, status in leads.items() if status in pending)
    return Reconciliation(
        distinct, counts, duplicates, [p for p in problems if p.startswith("call identity")], problems
    )


def reconcile(store: Store, job_id: str, *, final: bool = False) -> Reconciliation:
    intake = store.rows(
        "SELECT raw_count, distinct_count, duplicates, malformed FROM id_intake WHERE job_id=?", (job_id,)
    )
    leads = store.rows(
        "SELECT lead_id, status, calls_pulled, calls_sent, calls_dropped_cap, calls_dropped_unusable "
        "FROM leads WHERE job_id=?",
        (job_id,),
    )
    results = store.rows("SELECT r.lead_id, r.attempt_id FROM results r WHERE r.job_id=?", (job_id,))
    attempts = store.rows(
        "SELECT a.lead_id, a.attempt_id FROM attempts a LEFT JOIN leads l ON l.job_id=a.job_id AND l.lead_id=a.lead_id WHERE a.job_id=? AND l.lead_id IS NULL",
        (job_id,),
    )
    result_rows = [tuple(row) for row in results]
    result_rows.extend((f"orphan:{row[0]}", str(row[1])) for row in attempts)
    result = reconcile_rows(leads, result_rows, intake[0] if intake else None, final=final)
    ok_attempts = store.rows(
        "SELECT lead_id, attempt_id FROM attempts WHERE job_id=? AND outcome='ok'", (job_id,)
    )
    actual = {(str(row[0]), str(row[1])) for row in ok_attempts}
    for lead, attempt in result_rows:
        if lead in {str(row[0]) for row in leads} and (lead, attempt) not in actual:
            result.problems.append(f"result attempt is not the ok attempt for {lead}")
    return result
