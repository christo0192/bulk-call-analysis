from __future__ import annotations

from dataclasses import dataclass

from .db import Store


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


def reconcile(store: Store, job_id: str) -> Reconciliation:
    intake = store.rows(
        "SELECT raw_count, distinct_count, duplicates, malformed FROM id_intake WHERE job_id=?", (job_id,)
    )
    distinct = int(intake[0][1]) if intake else 0
    duplicates = int(intake[0][2]) if intake else 0
    rows = store.rows("SELECT status, count(*) AS n FROM leads WHERE job_id=? GROUP BY status", (job_id,))
    counts = {str(row[0]): int(row[1]) for row in rows}
    problems: list[str] = []
    total = sum(counts.values())
    if total != distinct:
        problems.append(f"lead identity mismatch: {distinct} distinct ids in, {total} leads stored")
    for row in store.rows(
        "SELECT lead_id, calls_pulled, calls_sent, calls_dropped_cap FROM leads WHERE job_id=?", (job_id,)
    ):
        if row[1] != row[2] + row[3]:
            problems.append(f"call identity mismatch for {row[0]}")
    if store.rows(
        "SELECT lead_id, count(*) FROM leads WHERE job_id=? GROUP BY lead_id HAVING count(*) > 1", (job_id,)
    ):
        problems.append("duplicate lead rows")
    if intake and intake[0][0] != intake[0][1] + intake[0][2] + intake[0][3]:
        problems.append("id intake counts do not reconcile")
    return Reconciliation(
        distinct, counts, duplicates, [p for p in problems if p.startswith("call identity")], problems
    )
