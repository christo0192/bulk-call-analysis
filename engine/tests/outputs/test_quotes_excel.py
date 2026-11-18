from pathlib import Path

from openpyxl import load_workbook  # type: ignore[import-untyped]

from bca.outputs.excel import ColumnSpec, write_results
from bca.outputs.quotes import check_answer, check_quote
from bca.store import QuoteField

LABELS = {"learner": ["Customer"], "advisor": ["Agent"]}


def test_quotes_exact_normalised_wrong_speaker_and_near_miss() -> None:
    text = "Customer: Hello   WORLD\nAgent: Other text"
    assert check_quote("hello world", text, "learner", LABELS).ok
    assert check_quote("HELLO WORLD", text, "learner", LABELS).ok
    assert check_quote("hello world", text, "advisor", LABELS).reason == "wrong speaker"
    near_miss = check_quote("hello worlx", text, "learner", LABELS)
    assert not near_miss.ok
    assert near_miss.reason == "near miss"
    assert "ratio=" in near_miss.diag


def test_quote_call_scope() -> None:
    bundle = "=== CALL 1 of 2\nCustomer: first\n=== CALL 2 of 2\nCustomer: second"
    checks = check_answer(
        {"quote": "second", "call_number": 2}, (QuoteField("quote", "learner"),), bundle, LABELS
    )
    assert checks[0].ok


def test_excel_formula_guard_numbers_and_header(tmp_path: Path) -> None:
    path = tmp_path / "values.xlsx"
    write_results(
        path,
        [ColumnSpec("a very long header that must remain intact"), ColumnSpec("number", value_type="number")],
        [{"a very long header that must remain intact": "=1+1", "number": 4}],
        "Results",
    )
    sheet = load_workbook(path, read_only=True, data_only=False)["Results"]
    rows = list(sheet.values)
    assert rows[1][0] == "=1+1"
    assert sheet["A2"].data_type == "s"
    assert rows[1][1] == 4


def test_excel_write_only_path_handles_many_rows(tmp_path: Path) -> None:
    path = tmp_path / "many.xlsx"
    write_results(
        path,
        [ColumnSpec("lead id"), ColumnSpec("number", value_type="number")],
        ({"lead id": f"lead-{index}", "number": index} for index in range(10_000)),
        "Results",
    )
    sheet = load_workbook(path, read_only=True, data_only=False)["Results"]
    rows = list(sheet.values)
    assert len(rows) == 10_001
    assert rows[-1] == ("lead-9999", 9999)
