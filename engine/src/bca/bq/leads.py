from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from openpyxl import load_workbook

from bca.store import LeadStatus, Store
from bca.store.spec import LeadSourceIds, LeadSourceSql

from .queries import lead_source_sql
from .runner import BqRunner

if TYPE_CHECKING:
    from collections.abc import Iterable


@dataclass(frozen=True)
class IdIntake:
    raw_count: int
    ids: list[str]
    duplicates: int
    malformed: list[str]


_ID = re.compile(r"^\d+$")


def _intake(values: Iterable[object]) -> IdIntake:
    raw = 0
    duplicate = 0
    malformed: list[str] = []
    ids: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value).strip()
        if not text:
            continue
        raw += 1
        if not _ID.fullmatch(text):
            malformed.append(text)
        elif text in seen:
            duplicate += 1
        else:
            seen.add(text)
            ids.append(text)
    return IdIntake(raw, ids, duplicate, malformed)


def _rows_csv(text: str) -> list[object]:
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return []
    headers = [cell.strip().lower() for cell in rows[0]]
    preferred = next((i for i, name in enumerate(headers) if name in {"lead_id", "id", "hubspot_id"}), None)
    if preferred is not None:
        return [row[preferred] for row in rows[1:] if len(row) > preferred]
    columns = list(zip(*rows, strict=False))
    candidates = [
        column for column in columns if any(_ID.fullmatch(cell.strip()) for cell in column if cell.strip())
    ]
    return list(candidates[0]) if candidates else []


def parse_id_list(path_or_text: str | Path) -> IdIntake:
    path = Path(path_or_text)
    if path.is_file():
        suffix = path.suffix.lower()
        if suffix == ".xlsx":
            workbook = load_workbook(path, read_only=True, data_only=True)
            try:
                active = workbook.active
                assert active is not None
                rows = list(active.iter_rows(values_only=True))
            finally:
                workbook.close()
            if not rows:
                return IdIntake(0, [], 0, [])
            headers = [str(x).strip().lower() if x is not None else "" for x in rows[0]]
            preferred = next(
                (i for i, name in enumerate(headers) if name in {"lead_id", "id", "hubspot_id"}), None
            )
            if preferred is None:
                preferred = next(
                    (
                        i
                        for i, col in enumerate(zip(*rows, strict=False))
                        if any(_ID.fullmatch(str(x).strip()) for x in col if x is not None)
                    ),
                    0,
                )
            return _intake(row[preferred] for row in rows[1:] if len(row) > preferred)
        text = path.read_text(encoding="utf-8")
        return _intake(_rows_csv(text) if suffix == ".csv" else re.split(r"[\s,]+", text))
    return _intake(re.split(r"[\s,]+", str(path_or_text)))


class LeadSourceError(ValueError):
    """Raised when a lead source cannot be safely consumed."""


def check_single_id_column(runner: BqRunner, user_sql: str) -> None:
    sql, params, _ = lead_source_sql(user_sql)
    result = runner.dry_run(sql, params)
    if result.schema != [("lead_id", "STRING")]:
        raise LeadSourceError("lead source must return exactly one STRING lead_id column")


def resolve_lead_source(
    spec: LeadSourceSql | LeadSourceIds,
    runner: BqRunner,
    store: Store,
    job_dir: Path,
    job_id: str | None = None,
) -> IdIntake:
    effective_job = job_id or job_dir.name
    if isinstance(spec, LeadSourceIds):
        intake = parse_id_list(job_dir / spec.ids_file)
    else:
        check_single_id_column(runner, spec.sql)
        sql, params, _ = lead_source_sql(spec.sql)
        values: list[object] = []
        for page in runner.iter_pages(sql, params, 1000):
            values.extend(row.get("lead_id", "") for row in page)
        intake = _intake(values)
    with store.transaction() as cur:
        cur.execute(
            "INSERT OR REPLACE INTO id_intake(job_id, raw_count, distinct_count, duplicates, malformed) "
            "VALUES (?, ?, ?, ?, ?)",
            (effective_job, intake.raw_count, len(intake.ids), intake.duplicates, len(intake.malformed)),
        )
    for lead_id in intake.ids:
        store.add_lead(effective_job, lead_id, LeadStatus.pending)
    return intake
