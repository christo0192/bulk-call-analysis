from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from openpyxl import load_workbook

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
    reconciliation = reconcile(store, job_id, final=True)
    problems = list(reconciliation.problems)
    workbook = load_workbook(workbook_path, read_only=True, data_only=False)
    try:
        if "Results" not in workbook.sheetnames:
            problems.append("missing Results sheet")
            return VerifyReport(False, tuple(problems), reconciliation)
        for name in workbook.sheetnames:
            for worksheet_row in workbook[name].iter_rows():
                for cell in worksheet_row:
                    if cell.data_type == "f":
                        problems.append(f"formula-like cell in {name} is not stored as a string")
        sheet = workbook["Results"]
        rows = list(sheet.values)
        headers = [str(value) for value in rows[0]] if rows else []
        required = {
            "lead id",
            "metadata",
            "model used",
            "prompt version",
            "flags",
            "quote checks ok",
            "calls read",
            "calls not read",
        }
        if not required.issubset(headers):
            problems.append("Results headers do not contain the required columns")
        answer_keys: set[str] = set()
        for stored in store.rows("SELECT answer_json FROM results WHERE job_id=?", (job_id,)):
            value = json.loads(str(stored[0]))
            if isinstance(value, dict):
                answer_keys.update(str(key) for key in value)
        if not answer_keys.issubset(headers):
            problems.append("Results headers are missing answer columns")
        lead_index = headers.index("lead id") if "lead id" in headers else -1
        done_rows = store.rows(
            "SELECT lead_id, calls_sent, calls_dropped_cap FROM leads "
            "WHERE job_id=? AND status='done' ORDER BY lead_id",
            (job_id,),
        )
        done = {str(row[0]) for row in done_rows}
        actual = [str(row[lead_index]) for row in rows[1:] if lead_index >= 0]
        if len(actual) != len(done):
            problems.append(f"workbook row count {len(actual)} does not equal done lead count {len(done)}")
        if set(actual) != done:
            problems.append("workbook lead identities do not equal done leads")
        if len(actual) != len(set(actual)):
            problems.append("duplicate done lead in workbook")
        result_rows = {
            str(row[0]): json.loads(str(row[1]))
            for row in store.rows("SELECT lead_id, answer_json FROM results WHERE job_id=?", (job_id,))
        }
        for row_values in rows[1:]:
            if lead_index < 0 or str(row_values[lead_index]) not in result_rows:
                continue
            lead_id = str(row_values[lead_index])
            answer = result_rows[lead_id]
            for key, value in answer.items():
                if key in headers and row_values[headers.index(key)] != value:
                    problems.append(f"cell mismatch for {lead_id}/{key}")
        return VerifyReport(not problems, tuple(problems), reconciliation)
    finally:
        workbook.close()
