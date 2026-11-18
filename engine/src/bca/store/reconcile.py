from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .db import Store


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
) -> Reconciliation:
    counts: dict[str, int] = {}
    for row in lead_rows:
        status = str(row[1])
        counts[status] = counts.get(status, 0) + 1
    distinct = _as_int(intake[1]) if intake else 0
    duplicates = _as_int(intake[2]) if intake else 0
    problems: list[str] = []
    total = sum(counts.values())
    if total != distinct:
        problems.append(f"lead identity mismatch: {distinct} distinct ids in, {total} leads stored")
    for row in lead_rows:
        if _as_int(row[2]) != _as_int(row[3]) + _as_int(row[4]):
            problems.append(f"call identity mismatch for {row[0]}")
    if result_rows:
        problems.append("duplicate lead rows")
    if intake and _as_int(intake[0]) != _as_int(intake[1]) + _as_int(intake[2]) + _as_int(intake[3]):
        problems.append("id intake counts do not reconcile")
    return Reconciliation(
        distinct, counts, duplicates, [p for p in problems if p.startswith("call identity")], problems
    )


def reconcile(store: Store, job_id: str) -> Reconciliation:
    intake_rows = store.rows(
        "SELECT raw_count, distinct_count, duplicates, malformed FROM id_intake WHERE job_id=?", (job_id,)
    )
    lead_rows = store.rows(
        "SELECT lead_id, status, calls_pulled, calls_sent, calls_dropped_cap FROM leads WHERE job_id=?",
        (job_id,),
    )
    result_rows = store.rows(
        "SELECT lead_id, count(*) FROM leads WHERE job_id=? GROUP BY lead_id HAVING count(*) > 1", (job_id,)
    )
    return reconcile_rows(lead_rows, result_rows, intake_rows[0] if intake_rows else None)
