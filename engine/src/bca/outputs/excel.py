from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from openpyxl import Workbook, load_workbook  # type: ignore[import-untyped]
from openpyxl.cell.cell import WriteOnlyCell  # type: ignore[import-untyped]

from bca.store import JobSpec, Store

from .rules import evaluate


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    field: str | None = None
    value_type: str = "string"


def _cell(ws: Any, value: object, numeric: bool) -> WriteOnlyCell:
    cell = WriteOnlyCell(ws, value)
    if numeric:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"numeric cell received {type(value).__name__}")
    else:
        cell.value = "" if value is None else str(value)
    cell.data_type = "n" if numeric else "s"
    return cell


def write_results(
    path: Path, columns: list[ColumnSpec], rows: Iterable[dict[str, object]], sheet_title: str
) -> None:
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet(sheet_title)
    headers = [column.name for column in columns]
    sheet.append([_cell(sheet, header, False) for header in headers])
    for cell in headers:
        _ = cell
    for row in rows:
        sheet.append(
            [
                _cell(sheet, row.get(column.field or column.name), column.value_type in ("number", "integer"))
                for column in columns
            ]
        )
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = f"A1:{chr(64 + len(headers))}{sheet.max_row}" if len(headers) <= 26 else None
    for index, header in enumerate(headers, 1):
        sheet.column_dimensions[chr(64 + index)].width = min(60, max(12, len(header) + 2))
    workbook.save(path)


def _spec_rows(
    store: Store, job_id: str, spec: JobSpec, rules: object = None
) -> tuple[list[ColumnSpec], list[dict[str, object]], list[dict[str, object]]]:
    rule_set = spec.rules if rules is None else cast(Any, rules)
    rows: list[dict[str, object]] = []
    not_read: list[dict[str, object]] = []
    leads = store.rows(
        "SELECT lead_id, status, reason, calls_pulled, calls_sent, calls_dropped_cap "
        "FROM leads WHERE job_id=? ORDER BY lead_id",
        (job_id,),
    )
    result_map = {
        str(row[0]): row
        for row in store.rows(
            "SELECT lead_id, answer_json, quotes_ok, flags_json FROM results WHERE job_id=?", (job_id,)
        )
    }
    for lead in leads:
        lead_id, status = str(lead[0]), str(lead[1])
        if status != "done":
            not_read.append({"lead id": lead_id, "bucket": status, "reason": lead[2] or "not done"})
            continue
        result = result_map.get(lead_id)
        if result is None:
            continue
        answer = json.loads(str(result[1]))
        values = dict(answer) if isinstance(answer, dict) else {}
        values.update(
            {
                "lead id": lead_id,
                "calls read": lead[4],
                "calls not read": lead[5],
                "quote checks ok": bool(result[2]),
                "rule results": json.dumps(evaluate(rule_set, answer, {"lead_id": lead_id}), sort_keys=True),
            }
        )
        rows.append(values)
    fields = [ColumnSpec("lead id", "lead id")]
    fields.extend(
        ColumnSpec(
            name, name, str(definition.get("type", "string")) if isinstance(definition, Mapping) else "string"
        )
        for name, definition in cast_mapping(spec.output_schema.get("properties", {})).items()
    )
    fields.extend(
        [
            ColumnSpec("rule results", "rule results"),
            ColumnSpec("quote checks ok", "quote checks ok", "boolean"),
            ColumnSpec("calls read", "calls read", "integer"),
            ColumnSpec("calls not read", "calls not read", "integer"),
        ]
    )
    return fields, rows, not_read


def cast_mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def build_workbook(store: Store, job_id: str, spec: JobSpec, out_dir: Path, version: int) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"results.v{version}.xlsx"
    columns, rows, not_read = _spec_rows(store, job_id, spec)
    write_results(path, columns, rows, "Results")
    workbook = load_workbook(path)
    sheet = workbook.create_sheet("Not read")
    sheet.append(["lead id", "bucket", "reason"])
    for row in not_read:
        sheet.append([row["lead id"], row["bucket"], row["reason"]])
    about = workbook.create_sheet("About")
    about.append(["job title", spec.title])
    about.append(["question", spec.question])
    about.append(["spec version", spec.spec_version])
    about.append(["rules", json.dumps([rule.to_dict() for rule in spec.rules], sort_keys=True)])
    workbook.save(path)
    return path


def rebuild_with_rules(store: Store, job_id: str, new_rules: tuple[object, ...], out_dir: Path) -> Path:
    old_paths = sorted(out_dir.glob("results.v*.xlsx"))
    if not old_paths:
        raise FileNotFoundError("no existing workbook to rebuild")
    old = load_workbook(old_paths[-1], read_only=True, data_only=False)
    source = old["Results"]
    values = list(source.values)
    if not values:
        raise ValueError("existing Results sheet has no header")
    headers = [str(item) for item in values[0]]
    rule_index = headers.index("rule results") if "rule results" in headers else -1
    if rule_index < 0:
        raise ValueError("existing Results sheet has no rule results column")
    columns = [
        ColumnSpec(header, header, "integer" if header in ("calls read", "calls not read") else "string")
        for header in headers
    ]
    result_rows = store.rows("SELECT lead_id, answer_json FROM results WHERE job_id=?", (job_id,))
    answers = {str(row[0]): json.loads(str(row[1])) for row in result_rows}
    output: list[dict[str, object]] = []
    for row in values[1:]:
        data = {header: value for header, value in zip(headers, row, strict=False)}
        lead_id = str(data["lead id"])
        answer = answers.get(lead_id, {})
        data["rule results"] = json.dumps(
            evaluate(cast(Any, new_rules), cast(dict[str, object], answer), {"lead_id": lead_id}),
            sort_keys=True,
        )
        output.append(data)
    version = (
        max(
            (
                int(path.stem.rsplit("v", 1)[-1])
                for path in old_paths
                if path.stem.rsplit("v", 1)[-1].isdigit()
            ),
            default=0,
        )
        + 1
    )
    target = out_dir / f"results.v{version}.xlsx"
    write_results(target, columns, output, "Results")
    return target
