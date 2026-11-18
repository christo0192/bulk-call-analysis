from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from openpyxl import load_workbook  # type: ignore[import-untyped]

from bca.store import Store, reconcile


@dataclass(frozen=True)
class VerifyReport:
    ok: bool
    problems: tuple[str, ...]
    reconciliation: object

    def to_markdown(self) -> str:
        lines = [
            "# Verify report",
            "",
            f"Status: {'pass' if self.ok else 'fail'}",
            "",
            "Claude spot-check: not run.",
        ]
        if self.problems:
            lines.extend(["", "Problems:", *[f"- {problem}" for problem in self.problems]])
        return "\n".join(lines) + "\n"


def verify_job(store: Store, job_id: str, workbook_path: Path) -> VerifyReport:
    reconciliation = reconcile(store, job_id)
    problems = list(reconciliation.problems)
    workbook = load_workbook(workbook_path, read_only=True, data_only=False)
    if "Results" not in workbook.sheetnames:
        problems.append("missing Results sheet")
        return VerifyReport(False, tuple(problems), reconciliation)
    sheet = workbook["Results"]
    rows = list(sheet.values)
    headers = [str(value) for value in rows[0]] if rows else []
    lead_index = headers.index("lead id") if "lead id" in headers else -1
    done = {
        str(row[0])
        for row in store.rows("SELECT lead_id FROM leads WHERE job_id=? AND status='done'", (job_id,))
    }
    actual = [str(row[lead_index]) for row in rows[1:] if lead_index >= 0]
    if len(actual) != len(done):
        problems.append(f"workbook row count {len(actual)} does not equal done lead count {len(done)}")
    if set(actual) != done:
        problems.append("workbook lead identities do not equal done leads")
    if len(actual) != len(set(actual)):
        problems.append("duplicate done lead in workbook")
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            if cell.data_type == "f":
                problems.append("formula-like cell is not stored as a string")
    return VerifyReport(not problems, tuple(problems), reconciliation)
