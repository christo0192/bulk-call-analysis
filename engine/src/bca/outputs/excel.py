from __future__ import annotations

import json
import math
import os
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from openpyxl import Workbook, load_workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE, Cell, WriteOnlyCell
from openpyxl.utils import get_column_letter

from bca.store import JobSpec, Store

from .rules import RuleTypeError, evaluate


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    field: str | None = None
    value_type: str = "string"


def _safe_string(value: object, flags: list[str], label: str) -> str:
    if isinstance(value, (list, dict)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    elif value is None:
        value = ""
    else:
        value = str(value)
    if len(value) > 32767:
        flags.append(f"{label}:truncated")
        value = value[:32760] + "...[truncated]"
    cleaned = ILLEGAL_CHARACTERS_RE.sub("", value)
    if cleaned != value:
        flags.append(f"{label}:illegal-character-removed")
    return str(cleaned)


def _cell(
    ws: Any, value: object, numeric: bool, flags: list[str] | None = None, label: str = "value"
) -> Cell:
    flags = flags if flags is not None else []
    if numeric is False and isinstance(value, bool):
        cell = WriteOnlyCell(ws, value)
        cell.data_type = "b"
        return cell
    if (
        numeric
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    ):
        cell = WriteOnlyCell(ws, value)
        cell.data_type = "n"
        return cell
    if numeric and value not in (None, ""):
        flags.append(f"{label}:invalid-number")
    cell = WriteOnlyCell(ws, _safe_string(value, flags, label))
    cell.data_type = "s"
    return cell


def write_results(
    path: Path, columns: list[ColumnSpec], rows: Iterable[dict[str, object]], sheet_title: str
) -> None:
    materialized_rows = list(rows)
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet(sheet_title)
    headers = [column.name for column in columns]
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{len(materialized_rows) + 1}"
    for index, header in enumerate(headers, 1):
        sheet.column_dimensions[get_column_letter(index)].width = min(60, max(12, len(header) + 2))
    sheet.append([_cell(sheet, header, False) for header in headers])
    for row in materialized_rows:
        row_flags = row.get("flags", [])
        flags = row_flags if isinstance(row_flags, list) else []
        cells: list[Cell | None] = []
        deferred_flags: list[int] = []
        for index, column in enumerate(columns):
            if column.name == "flags":
                cells.append(None)
                deferred_flags.append(index)
            else:
                cells.append(
                    _cell(
                        sheet,
                        row.get(column.field or column.name),
                        column.value_type in ("number", "integer"),
                        flags,
                        column.name,
                    )
                )
        for index in deferred_flags:
            cells[index] = _cell(sheet, json.dumps(flags), False, [], "flags")
        sheet.append(cast(list[Cell], cells))

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.stem}.", suffix=path.suffix, delete=False
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
        workbook.save(str(temporary_path))
        os.replace(str(temporary_path), str(path))
        temporary_path = None
    finally:
        workbook.close()
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def cast_mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _metadata(lead: Any) -> dict[str, object]:
    return {
        "lead_id": str(lead[0]),
        "status": str(lead[1]),
        "reason": lead[2],
        "calls_pulled": lead[3],
        "calls_sent": lead[4],
        "calls_dropped_cap": lead[5],
        "calls_dropped_unusable": lead[6],
    }


def _rule_names(rules: Iterable[object]) -> list[str]:
    names: list[str] = []
    for rule in rules:
        rule_value = cast(Any, rule)
        names.append("in_scope" if rule_value.kind == "in_scope" else str(rule_value.name))
    return names


def _spec_rows(
    store: Store, job_id: str, spec: JobSpec, rules: object = None
) -> tuple[list[ColumnSpec], list[dict[str, object]], list[dict[str, object]]]:
    rule_set = spec.rules if rules is None else cast(Any, rules)
    rows: list[dict[str, object]] = []
    not_read: list[dict[str, object]] = []
    leads = store.rows(
        "SELECT lead_id, status, reason, calls_pulled, calls_sent, "
        "calls_dropped_cap, calls_dropped_unusable FROM leads WHERE job_id=? ORDER BY lead_id",
        (job_id,),
    )
    result_map = {
        str(row[0]): row
        for row in store.rows(
            "SELECT lead_id, answer_json, quotes_ok, flags_json, attempt_id FROM results WHERE job_id=?",
            (job_id,),
        )
    }
    rule_names = _rule_names(rule_set)
    for lead in leads:
        lead_id, status = str(lead[0]), str(lead[1])
        if status != "done":
            not_read.append({"lead id": lead_id, "bucket": status, "reason": lead[2] or "not done"})
            continue
        result = result_map.get(lead_id)
        if result is None:
            continue
        answer = json.loads(str(result[1]))
        answer = answer if isinstance(answer, dict) else {}
        flags = json.loads(str(result[3])) if result[3] else []
        flags = [str(x) for x in flags] if isinstance(flags, list) else ["flags:not-a-list"]
        try:
            rule_result = evaluate(rule_set, answer, {"lead_id": lead_id})
        except RuleTypeError as exc:
            rule_result = {name: "rule error" for name in rule_names}
            flags.append(f"rule-error:{exc}")
        values = dict(answer)
        attempt_rows = store.rows(
            "SELECT model_used, spec_version FROM attempts WHERE attempt_id=?", (result[4],)
        )
        values.update(
            {
                "lead id": lead_id,
                "metadata": json.dumps(_metadata(lead), ensure_ascii=False, sort_keys=True),
                "model used": attempt_rows[0][0] if attempt_rows else "",
                "prompt version": attempt_rows[0][1] if attempt_rows else "",
                "flags": json.dumps(flags),
                "quote checks ok": bool(result[2]),
                "calls read": lead[4],
                "calls not read": lead[5],
                "flags_list": flags,
            }
        )
        values.update({name: rule_result.get(name, "unknown") for name in rule_names})
        rows.append(values)
    fields = [
        ColumnSpec("lead id", "lead id"),
        ColumnSpec("metadata", "metadata"),
        ColumnSpec("model used", "model used"),
        ColumnSpec("prompt version", "prompt version"),
        ColumnSpec("flags", "flags"),
    ]
    for name, definition in cast_mapping(spec.output_schema.get("properties", {})).items():
        schema_type = definition.get("type", "string") if isinstance(definition, Mapping) else "string"
        value_type = (
            next((item for item in schema_type if item != "null"), "string")
            if isinstance(schema_type, list)
            else str(schema_type)
        )
        fields.append(ColumnSpec(name, name, value_type))
    fields.extend(ColumnSpec(name, name) for name in rule_names)
    fields.extend(
        [
            ColumnSpec("quote checks ok", "quote checks ok", "boolean"),
            ColumnSpec("calls read", "calls read", "integer"),
            ColumnSpec("calls not read", "calls not read", "integer"),
        ]
    )
    return fields, rows, not_read


def _write_side_sheets(path: Path, spec: JobSpec, not_read: list[dict[str, object]], note: str = "") -> None:
    workbook = load_workbook(path)
    try:
        sheet = workbook.create_sheet("Not read")
        sheet.append(["lead id", "bucket", "reason"])
        for row in not_read:
            flags: list[str] = []
            sheet.append(
                [
                    _cell(sheet, row["lead id"], False, flags, "lead id"),
                    _cell(sheet, row["bucket"], False, flags, "bucket"),
                    _cell(sheet, row["reason"], False, flags, "reason"),
                ]
            )
        about = workbook.create_sheet("About")
        about_rows: list[list[object]] = [
            ["job title", spec.title],
            ["question", spec.question],
            ["spec version", spec.spec_version],
            ["rules", json.dumps([rule.to_dict() for rule in spec.rules], sort_keys=True)],
            ["note", note],
        ]
        for about_row in about_rows:
            about.append([_cell(about, item, False) for item in about_row])
        workbook.save(str(path))
    finally:
        workbook.close()


def build_workbook(store: Store, job_id: str, spec: JobSpec, out_dir: Path, version: int) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"results.v{version}.xlsx"
    columns, rows, not_read = _spec_rows(store, job_id, spec)
    store.append_event(job_id, "spec", spec.to_dict())
    write_results(path, columns, rows, "Results")
    _write_side_sheets(path, spec, not_read)
    return path


def rebuild_with_rules(store: Store, job_id: str, new_rules: tuple[object, ...], out_dir: Path) -> Path:
    paths = list(out_dir.glob("results.v*.xlsx"))
    numbered = [
        (int(path.stem.rsplit("v", 1)[-1]), path) for path in paths if path.stem.rsplit("v", 1)[-1].isdigit()
    ]
    if not numbered:
        raise FileNotFoundError("no existing workbook to rebuild")
    source = max(numbered, key=lambda item: item[0])[1]
    old = load_workbook(source, read_only=True, data_only=False)
    try:
        old_sheet_names = old.sheetnames
    finally:
        old.close()
    if set(old_sheet_names) != {"Results", "Not read", "About"}:
        raise ValueError("existing workbook has unexpected sheets")
    spec_row = store.rows(
        "SELECT data_json FROM job_events WHERE job_id=? AND kind='spec' ORDER BY id DESC LIMIT 1", (job_id,)
    )
    if spec_row:
        spec = JobSpec.from_dict(json.loads(str(spec_row[0][0])))
    else:
        raise ValueError("stored spec is required for rebuild")
    spec = JobSpec.from_dict(spec.to_dict() | {"rules": [cast(Any, rule).to_dict() for rule in new_rules]})
    columns, rows, not_read = _spec_rows(store, job_id, spec, new_rules)
    target = out_dir / f"results.v{max(item[0] for item in numbered) + 1}.xlsx"
    write_results(target, columns, rows, "Results")
    _write_side_sheets(target, spec, not_read, "rebuilt with new rules")
    store.append_event(job_id, "rules_rebuilt", {"rules": [cast(Any, rule).to_dict() for rule in new_rules]})
    return target
