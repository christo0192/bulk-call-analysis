from pathlib import Path

from openpyxl import load_workbook

from bca.outputs.excel import ColumnSpec, write_results
from bca.outputs.quotes import QuoteCheck, check_answer, check_quote, repair_prompt
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


def test_quotes_require_one_line_words_case_insensitive_labels_and_continuations() -> None:
    text = "=== CALL 2 of 2 === · date · 3 min ===\n[00:01:02] customer: I can't\n afford it\nAgent: okay"
    assert check_quote("I can\u2019t afford it", text, "learner", LABELS).ok
    assert not check_quote("able to pay", "Customer: I am unable to pay", "learner", LABELS).ok
    splice = "Customer: I want to\nAgent: Go on\nCustomer: quit my job soon"
    assert not check_quote("I want to quit my job", splice, "learner", LABELS).ok


def test_quote_call_number_coercion_and_repair_prompt_context() -> None:
    bundle = "=== CALL 1 of 2 ===\nCustomer: first words\n=== CALL 2 of 2 ===\nCustomer: second words"
    field = (QuoteField("objection", "learner"),)
    assert not check_answer({"objection": "first words", "call_number": "2"}, field, bundle, LABELS)[0].ok
    assert check_answer({"objection": "second words", "call_number": 2.0}, field, bundle, LABELS)[0].ok
    failure = QuoteCheck(False, "not found", "diagnostic", "objection", 2)
    prompt = repair_prompt({"objection": "quoted text", "valid": 1}, [failure], "INSTR")
    assert all(item in prompt for item in ("objection", "quoted text", "call=2", "valid"))


def test_excel_formula_guard_numbers_and_header(tmp_path: Path) -> None:
    path = tmp_path / "values.xlsx"
    write_results(
        path,
        [ColumnSpec("a very long header that must remain intact"), ColumnSpec("number", value_type="number")],
        [{"a very long header that must remain intact": "=1+1", "number": 4}],
        "Results",
    )
    assert path.exists()
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
    assert path.exists()
    sheet = load_workbook(path, read_only=True, data_only=False)["Results"]
    rows = list(sheet.values)
    assert len(rows) == 10_001
    assert rows[-1] == ("lead-9999", 9999)


def test_a_timestamp_after_the_speaker_label_is_read() -> None:
    # Some call systems write "Speaker Prospect - 1 [0:00:01.119]: text": the timestamp follows
    # the label, and the label carries a speaker number.
    labels = {"learner": ["Speaker Prospect"], "advisor": ["Speaker Sales Rep"]}
    text = (
        "Speaker Prospect - 1 [0:00:01.119]: I lost my job last month\n"
        "Speaker Sales Rep [0:00:06.702]: In 30,000 we have 2 mock interviews,\n"
        "unlimited technical sessions\n"
        "Speaker Prospect - 2 [0:01:10.5]: the fee is too high"
    )
    assert check_quote("I lost my job last month", text, "learner", labels).ok
    assert check_quote("the fee is too high", text, "learner", labels).ok
    assert check_quote("2 mock interviews, unlimited technical sessions", text, "advisor", labels).ok
    assert check_quote("I lost my job last month", text, "advisor", labels).reason == "wrong speaker"
    assert check_quote("mock interviews", text, "learner", labels).reason == "wrong speaker"


def test_a_number_is_only_dropped_from_the_end_of_a_label() -> None:
    labels = {"learner": ["Customer"], "advisor": ["Agent"]}
    assert check_quote("hello there", "Customer 2: hello there", "learner", labels).ok
    assert check_quote("hello there", "[0:01] Agent - 1: hello there", "advisor", labels).ok
    # A different label that merely ends in a number is still a different speaker.
    other = check_quote("hello there", "Customer Care 2: hello there", "learner", labels)
    assert other.reason == "wrong speaker"


def test_long_runs_of_spaces_are_read_in_linear_time() -> None:
    # A transcript line padded with tens of thousands of spaces or tabs (an export artefact) took
    # tens of seconds per quote check before; each check now reads it in well under a second.
    import time

    labels = {"learner": ["Customer"], "advisor": ["Agent"]}
    for padding in (
        " " * 50_000 + "x",
        "\t" * 50_000,
        " " * 1_000 + "y" * 100_000,
        "Customer" + " " * 50_000,
    ):
        started = time.perf_counter()
        assert check_quote("hello there", padding + "\nCustomer: hello there", "learner", labels).ok
        assert time.perf_counter() - started < 2, repr(padding[:12])


def test_a_long_call_without_speaker_lines_is_read_in_linear_time() -> None:
    # 4,000 unlabelled lines after one speaker line (or a call header with colons) once took over
    # 100 s per quote check, because each line re-normalised all the text gathered so far.
    import time

    labels = {"learner": ["Customer"], "advisor": ["Agent"]}
    body = "\n".join(f"word {index} and some more words to fill the line" for index in range(4_000))
    for opener in ("Customer: start", "=== CALL 1 of 1 | 2026-07-06T10:00:00 | 900 s ==="):
        started = time.perf_counter()
        check_quote("word 3999 and some", opener + "\n" + body, "learner", labels)
        assert time.perf_counter() - started < 2, opener
    text = "Customer: I lost\nmy job\n  last month\nAgent: sorry"
    assert check_quote("I lost my job last month", text, "learner", labels).ok
