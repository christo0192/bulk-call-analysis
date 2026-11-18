from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest
from openpyxl import Workbook

from bca.bq.leads import LeadSourceError, parse_id_list, parse_id_text
from bca.bq.queries import lead_source_sql
from bca.bq.runner import DryRun


def test_txt_csv_and_xlsx_intake(tmp_path: Path) -> None:
    txt = tmp_path / "ids.txt"
    txt.write_text("12 13\n13 bad\n", encoding="utf-8")
    parsed = parse_id_list(txt)
    assert (parsed.raw_count, parsed.ids, parsed.duplicates, parsed.malformed) == (
        4,
        ["12", "13"],
        1,
        ["bad"],
    )
    csv = tmp_path / "ids.csv"
    csv.write_text("lead_id,other\n21,x\n21,y\n", encoding="utf-8")
    assert parse_id_list(csv).ids == ["21"]
    xlsx = tmp_path / "ids.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.append(["other", "ID"])
    sheet.append(["x", 31])
    sheet.append(["y", "oops"])
    workbook.save(xlsx)
    result = parse_id_list(xlsx)
    assert result.ids == ["31"] and result.malformed == ["oops"]


class SchemaRunner:
    def dry_run(self, sql: str, params: Mapping[str, object] | None = None) -> DryRun:
        return DryRun(0, [], [("one", "STRING"), ("two", "STRING")])

    def iter_pages(
        self, sql: str, params: Mapping[str, object] | None = None, page_size: int = 1000
    ) -> Iterator[list[dict[str, object]]]:
        yield []


def test_single_id_column_check_rejects_two_columns() -> None:
    from bca.bq.leads import check_single_id_column

    with pytest.raises(LeadSourceError):
        check_single_id_column(SchemaRunner(), "SELECT 1")


def test_lead_source_parameterizes_user_sql() -> None:
    sql, params, _ = lead_source_sql("SELECT lead_id FROM source")
    assert params == {} and "@lead_ids" not in sql and "12345" not in sql


def test_single_id_column_checks_the_unwrapped_query() -> None:
    from bca.bq.leads import check_single_id_column

    class QueryAwareRunner(SchemaRunner):
        def dry_run(self, sql: str, params: Mapping[str, object] | None = None) -> DryRun:
            if "email" in sql:
                return DryRun(0, [], [("lead_id", "STRING"), ("email", "STRING")])
            return DryRun(0, [], [("lead_id", "STRING")])

    with pytest.raises(LeadSourceError):
        check_single_id_column(QueryAwareRunner(), "SELECT lead_id, email FROM contacts")


def test_id_intake_handles_bom_blank_rows_and_only_ascii_digits(tmp_path: Path) -> None:
    csv = tmp_path / "ids.csv"
    non_ascii = "\uff11\uff10\uff12"
    csv.write_text(f"\ufefflead_id\n101\n\n{non_ascii}\n", encoding="utf-8")
    result = parse_id_list(csv)
    assert result.ids == ["101"] and result.malformed == [non_ascii]
    assert parse_id_text("1" * 300).malformed == []


def test_headerless_xlsx_keeps_first_id(tmp_path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    for value in (101, 102, 103):
        sheet.append([value])
    path = tmp_path / "ids.xlsx"
    workbook.save(path)
    assert parse_id_list(path).ids == ["101", "102", "103"]
