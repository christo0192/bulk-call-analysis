from __future__ import annotations

import csv
import io
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from openpyxl import load_workbook

from bca.config import BigQueryConfig
from bca.store import LeadStatus, Store
from bca.store.spec import LeadSourceIds, LeadSourceSql

from .queries import lead_source_sql
from .runner import BqRunner, DryRun

if TYPE_CHECKING:
    from collections.abc import Iterable


@dataclass(frozen=True)
class IdIntake:
    raw_count: int
    ids: list[str]
    duplicates: int
    malformed: list[str]


_ID = re.compile(r"^[0-9]+$", re.ASCII)


def _intake(values: Iterable[object]) -> IdIntake:
    raw = 0
    duplicate = 0
    malformed: list[str] = []
    ids: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value is None:
            continue
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


def _rows_csv(text: str) -> list[str]:
    rows = [row for row in csv.reader(io.StringIO(text)) if any(cell.strip() for cell in row)]
    if not rows:
        return []
    headers = [cell.strip().lower() for cell in rows[0]]
    preferred = next((i for i, name in enumerate(headers) if name in {"lead_id", "id", "hubspot_id"}), None)
    if preferred is not None:
        return [row[preferred] for row in rows[1:] if len(row) > preferred]
    for index in range(max(len(row) for row in rows)):
        column = [row[index] if index < len(row) else "" for row in rows]
        if any(_ID.fullmatch(cell.strip()) for cell in column if cell.strip()):
            return column[1:] if not _ID.fullmatch(column[0].strip()) else column
    return []


def parse_id_file(path: Path) -> IdIntake:
    path = Path(path)
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
            first_is_id = (
                _ID.fullmatch(str(rows[0][preferred]).strip()) if rows[0][preferred] is not None else None
            )
            source_rows = rows if first_is_id else rows[1:]
            return _intake(row[preferred] for row in source_rows if len(row) > preferred)
        text = path.read_text(encoding="utf-8-sig")
        return _intake(_rows_csv(text) if suffix == ".csv" else re.split(r"[\s,]+", text))
    raise FileNotFoundError(path)


def parse_id_text(text: str) -> IdIntake:
    return _intake(re.split(r"[\s,]+", text.lstrip("\ufeff")))


def parse_id_list(path_or_text: str | Path) -> IdIntake:
    if isinstance(path_or_text, Path):
        return parse_id_file(path_or_text)
    return parse_id_text(path_or_text)


class QueryRunner(Protocol):
    def dry_run(self, sql: str, params: Mapping[str, object] | None = None) -> DryRun: ...


class LeadSourceError(ValueError):
    """Raised when a lead source cannot be safely consumed."""


def check_single_id_column(runner: QueryRunner, user_sql: str) -> None:
    result = runner.dry_run(user_sql.strip().removesuffix(";").rstrip(), {})
    if len(result.schema) != 1 or result.schema[0][1] != "STRING":
        raise LeadSourceError("lead source must return exactly one STRING lead_id column")


def resolve_lead_source(
    spec: LeadSourceSql | LeadSourceIds,
    runner: BqRunner,
    store: Store,
    job_dir: Path,
    cfg: BigQueryConfig,
    job_id: str | None = None,
    approved_bytes: int | None = None,
    persist: bool = True,
) -> IdIntake:
    effective_job = job_id or job_dir.name
    if isinstance(spec, LeadSourceIds):
        intake = parse_id_list(job_dir / spec.ids_file)
    else:
        check_single_id_column(runner, spec.sql)
        sql, params, _ = lead_source_sql(spec.sql)
        dry = runner.dry_run(sql, params)
        if dry.bytes > cfg.reject_bytes and (approved_bytes is None or approved_bytes < dry.bytes):
            raise PermissionError("scan exceeds reject_bytes without operator approval")
        values: list[object] = []
        for page in runner.iter_pages(sql, params, 1000):
            values.extend(row.get("lead_id", "") for row in page)
        intake = _intake(values)
    if persist:
        record_lead_intake(store, effective_job, intake)
    return intake


def record_lead_intake(
    store: Store, job_id: str, intake: IdIntake, *, replace_existing: bool = False
) -> None:
    """Persist an already validated intake after its job row exists."""
    with store.transaction() as cur:
        if replace_existing:
            # The intake is the authoritative set on a pre-run re-proposal.
            # Remove attempts before leads because attempts reference the lead pair.
            cur.execute("DELETE FROM results WHERE job_id=?", (job_id,))
            cur.execute("DELETE FROM attempts WHERE job_id=?", (job_id,))
            cur.execute("DELETE FROM leads WHERE job_id=?", (job_id,))
        cur.execute(
            "INSERT OR REPLACE INTO id_intake(job_id, raw_count, distinct_count, duplicates, malformed) "
            "VALUES (?, ?, ?, ?, ?)",
            (job_id, intake.raw_count, len(intake.ids), intake.duplicates, len(intake.malformed)),
        )
        for lead_id in intake.ids:
            cur.execute(
                "INSERT OR IGNORE INTO leads(job_id, lead_id, status) VALUES (?, ?, ?)",
                (job_id, lead_id, str(LeadStatus.pending)),
            )
            if replace_existing:
                cur.execute(
                    "UPDATE leads SET status=?, reason=NULL, in_sample=0 WHERE job_id=? AND lead_id=?",
                    (str(LeadStatus.pending), job_id, lead_id),
                )
