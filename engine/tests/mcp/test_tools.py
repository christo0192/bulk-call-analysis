from __future__ import annotations

import gzip
import json
import logging
import re
from pathlib import Path
from typing import cast

import pytest

from bca.bq.guard import sql_tokens
from bca.bq.runner import DryRun
from bca.mcp.host import ToolHost, ToolHostError
from bca.store import JobSpec, LeadSourceSql, LeadStatus, save_spec
from tests.fakes.fake_bq import FakeBqRunner
from tests.mcp.test_host import make_host, recipe_config


def test_bq_list_datasets_through_host(tmp_path: Path) -> None:
    assert make_host(tmp_path, FakeBqRunner(datasets=[{"dataset_id": "analytics"}])).call(
        "bq_list_datasets"
    ) == [{"dataset_id": "analytics"}]


def test_bq_list_tables_through_host(tmp_path: Path) -> None:
    runner = FakeBqRunner(tables={"analytics": [{"table_id": "events", "dataset": "analytics"}]})
    assert make_host(tmp_path, runner).call("bq_list_tables", {"dataset": "analytics"}) == [
        {"table_id": "events", "dataset": "analytics"}
    ]


def test_bq_list_tables_cap_includes_wrapper_and_marker(tmp_path: Path) -> None:
    tables: list[dict[str, object]] = [
        {"table_id": f"table_{index:04d}", "dataset": "analytics"} for index in range(3000)
    ]
    result = make_host(tmp_path, FakeBqRunner(tables={"analytics": tables})).call(
        "bq_list_tables", {"dataset": "analytics"}
    )
    assert len(json.dumps(result, ensure_ascii=False)) <= 40_000
    result_dict = cast(dict[str, object], result)
    assert re.fullmatch(r"\d+ of 3000 entries omitted", str(result_dict["truncated"]))


def test_bq_list_tables_rejects_invalid_identifier(tmp_path: Path) -> None:
    with pytest.raises(ToolHostError, match="invalid dataset"):
        make_host(tmp_path, FakeBqRunner()).call("bq_list_tables", {"dataset": "bad-name"})


def test_bq_describe_table_through_host(tmp_path: Path) -> None:
    result = make_host(
        tmp_path,
        FakeBqRunner(
            table_details={
                ("analytics", "events"): {
                    "dataset": "analytics",
                    "table": "events",
                    "schema": [],
                    "num_rows": 7,
                }
            }
        ),
    ).call("bq_describe_table", {"dataset": "analytics", "table": "events"})
    assert result == {"dataset": "analytics", "table": "events", "schema": [], "num_rows": 7}


def test_bq_describe_table_cap_reconciles_schema_entries(tmp_path: Path) -> None:
    schema = [{"name": f"column_{index}", "type": "STRING", "mode": "NULLABLE"} for index in range(1200)]
    result = make_host(
        tmp_path,
        FakeBqRunner(table_details={("analytics", "events"): {"schema": schema}}),
    ).call("bq_describe_table", {"dataset": "analytics", "table": "events"})
    result_dict = cast(dict[str, object], result)
    kept = cast(list[object], result_dict["schema"])
    marker = str(result_dict["schema_truncated"])
    match = re.fullmatch(r"(\d+) of (\d+) entries omitted", marker)
    assert match is not None
    assert len(kept) + int(match.group(1)) == int(match.group(2)) == 1200


@pytest.mark.parametrize(
    "format_sql",
    ["'id=%x'", "'%x '", "'%d-%x'", "'''%x'''", '"%\'x"', "'%*x'"],
)
def test_format_rejects_hex_and_octal_specifiers_anywhere(tmp_path: Path, format_sql: str) -> None:
    with pytest.raises(ToolHostError, match="reconstruct"):
        make_host(tmp_path).call("bq_query", {"sql": f"SELECT FORMAT({format_sql}, phone)"})


def test_raw_literals_skip_escaped_quote_for_reconstruction_checks(tmp_path: Path) -> None:
    single = sql_tokens(r"SELECT r'\'' AS value, TO_HEX(value)")
    double = sql_tokens(r'SELECT r"\"" AS value, TO_BASE64(value)')
    assert single[1].text == r"r'\''"
    assert double[1].text == r'r"\""'
    host = make_host(tmp_path)
    for sql in (r"SELECT r'\'' AS value, TO_HEX(value)", r'SELECT r"\"" AS value, TO_BASE64(value)'):
        with pytest.raises(ToolHostError, match="reconstruct"):
            host.call("bq_query", {"sql": sql})


def test_bq_dry_run_through_host(tmp_path: Path) -> None:
    result = make_host(tmp_path, FakeBqRunner(dry=DryRun(12, ["events"], [("id", "INT64")]))).call(
        "bq_dry_run", {"sql": "SELECT 1"}
    )
    assert result == {"bytes": "12 B", "referenced_tables": ["events"], "schema": [["id", "INT64"]]}


@pytest.mark.parametrize("bytes_used, expected", [(5 * 1024**3, "5.00 GiB"), (2 * 1024**4, "2.00 TiB")])
def test_bq_readable_sizes_are_consistent(tmp_path: Path, bytes_used: int, expected: str) -> None:
    runner = FakeBqRunner(dry=DryRun(bytes_used, [], []))
    host = make_host(tmp_path, runner)
    host.bq_config = host.bq_config.__class__("project", reject_bytes=3 * 1024**4)
    assert host.call("bq_dry_run", {"sql": "SELECT 1"})["bytes"] == expected  # type: ignore[index]
    assert host.call("bq_query", {"sql": "SELECT 1"})["bytes"] == expected  # type: ignore[index]


@pytest.mark.parametrize("bytes_used, expected", [(5 * 1024**3, "5.00 GiB"), (2 * 1024**4, "2.00 TiB")])
def test_propose_job_plan_card_formats_both_byte_fields(
    tmp_path: Path, bytes_used: int, expected: str
) -> None:
    runner = FakeBqRunner(
        pages_by_query_kind={
            "lead_source": [[{"lead_id": "1001"}]],
            "preflight": [
                [
                    {
                        "lead_id": "1001",
                        "found": True,
                        "calls": 1,
                        "transcript_calls": 1,
                        "usable_calls": 1,
                        "transcription_failed": 0,
                    }
                ]
            ],
        },
        dry=DryRun(bytes_used, [], []),
    )
    host = proposal_host(tmp_path, [], runner=runner)
    host.bq_config = host.bq_config.__class__("project", reject_bytes=3 * 1024**4)
    spec = json.loads(proposal_json())
    spec["lead_source"] = {"kind": "sql", "sql": "SELECT '1001' AS lead_id"}
    card = cast(dict[str, object], host.call("propose_job", {"spec_json": json.dumps(spec)}))
    preflight = cast(dict[str, object], card["preflight"])
    assert card["lead_source_dry_run_bytes"] == expected
    assert preflight["dry_run_bytes"] == expected


def test_sample_reference_round_trip_keeps_the_same_masked_lead(tmp_path: Path) -> None:
    host = make_host(tmp_path)
    host.store.create_job("job", "project", 1)
    save_spec(
        JobSpec(1, "job", "project", "title", "question", LeadSourceSql("SELECT 1"), output_schema={}),
        tmp_path / "jobs" / "job",
    )
    for lead_id, status in (
        ("a", LeadStatus.masked),
        ("b", LeadStatus.masked),
        ("held", LeadStatus.held_back),
    ):
        host.store.add_lead("job", lead_id, status, in_sample=1)
    with host.store.transaction() as cur:
        for lead_id in ("a", "b"):
            cur.execute(
                "INSERT INTO results VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("job", lead_id, "attempt", "{}", 1, "{}", "now", 1),
            )
    transcript_dir = tmp_path / "jobs" / "job" / "cache" / "transcripts"
    transcript_dir.mkdir(parents=True)
    for lead_id in ("a", "b"):
        with gzip.open(transcript_dir / f"{lead_id}.jsonl.gz", "wt", encoding="utf-8") as handle:
            handle.write(
                json.dumps({"call_id": lead_id, "call_start": "2026-01-01", "transcript_text": lead_id})
                + "\n"
            )

    rows = cast(
        list[dict[str, object]], host.call("sample_results", {"job_id": "job", "offset": 0, "limit": 1})
    )
    assert rows[0]["ref"] == "S1"
    before = host.call("lead_transcript", {"job_id": "job", "ref": "S1"})
    host.store.set_lead_status("job", "a", LeadStatus.in_flight)
    during = host.call("lead_transcript", {"job_id": "job", "ref": "S1"})
    host.store.set_lead_status("job", "a", LeadStatus.done)
    after = host.call("lead_transcript", {"job_id": "job", "ref": "S1"})
    assert before == during == after
    page_two = cast(
        list[dict[str, object]], host.call("sample_results", {"job_id": "job", "offset": 1, "limit": 1})
    )
    assert page_two[0]["ref"] == "S2"
    with pytest.raises(ToolHostError, match="unavailable"):
        host.call("lead_transcript", {"job_id": "job", "ref": "S3"})


def test_bq_query_rejects_more_than_1000_rows(tmp_path: Path) -> None:
    with pytest.raises(ToolHostError, match="between 1 and 1000"):
        make_host(tmp_path).call("bq_query", {"sql": "SELECT 1", "max_rows": 1001})


def test_bq_information_schema_rejects_disallowed_view(tmp_path: Path) -> None:
    with pytest.raises(ToolHostError, match="invalid information schema"):
        make_host(tmp_path).call("bq_information_schema", {"dataset": "analytics", "view": "ROUTINES"})


def test_data_notes_reports_missing_file(tmp_path: Path) -> None:
    assert make_host(tmp_path).call("data_notes") == "no notes yet"


def test_brief_update_and_read_through_host(tmp_path: Path) -> None:
    host = make_host(tmp_path)
    assert host.call("brief_update", {"text": "hello"}) == "updated"
    assert host.call("brief_read") == "hello"


def test_brief_update_enforces_20k_cap(tmp_path: Path) -> None:
    with pytest.raises(ToolHostError, match="20k"):
        make_host(tmp_path).call("brief_update", {"text": "x" * 20_001})


def test_propose_job_invalid_spec_is_tool_error(tmp_path: Path) -> None:
    with pytest.raises(ToolHostError) as error:
        make_host(tmp_path).call("propose_job", {"spec_json": "{}"})
    assert "Traceback" not in str(error.value)
    assert str(tmp_path) not in str(error.value)


def test_job_status_through_host(tmp_path: Path) -> None:
    host = make_host(tmp_path)
    host.store.create_job("job", "project", 1, "plan_proposed")
    host.store.add_lead("job", "lead", LeadStatus.masked, in_sample=1)
    result = host.call("job_status", {"job_id": "job"})
    assert result["state"] == "plan_proposed"  # type: ignore[index]


def test_result_aggregates_through_host(tmp_path: Path) -> None:
    host = make_host(tmp_path)
    host.store.create_job("job", "project", 1)
    save_spec(
        JobSpec(
            1,
            "job",
            "project",
            "title",
            "question",
            LeadSourceSql("SELECT 1"),
            output_schema={"type": "object", "properties": {"answer": {"type": "string"}}},
            luna_model="gpt-5.6-luna",
            luna_effort="high",
        ),
        tmp_path / "jobs" / "job",
    )
    assert host.call("result_aggregates", {"job_id": "job"}) == {
        "answer_fields": {"answer": {}},
        "rule_results": {},
    }


def test_sample_results_excludes_non_sample_leads(tmp_path: Path) -> None:
    host = make_host(tmp_path)
    host.store.create_job("job", "project", 1)
    host.store.add_lead("job", "lead", LeadStatus.done, in_sample=0)
    assert host.call("sample_results", {"job_id": "job"}) == []


def test_sample_results_uses_opaque_stable_references(tmp_path: Path) -> None:
    host = make_host(tmp_path)
    host.store.create_job("job", "project", 1)
    host.store.add_lead("job", "10000000001", LeadStatus.done, in_sample=1)
    host.store.add_lead("job", "10000000002", LeadStatus.done, in_sample=1)
    with host.store.transaction() as cur:
        for lead_id in ("10000000001", "10000000002"):
            cur.execute(
                "INSERT INTO results VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("job", lead_id, "attempt", "{}", 1, "{}", "now", 1),
            )
    result = host.call("sample_results", {"job_id": "job"})
    sample_rows = cast(list[dict[str, object]], result)
    assert [item["ref"] for item in sample_rows] == ["S1", "S2"]
    assert all("lead_id" not in item for item in sample_rows)


def test_lead_transcript_is_masked_and_capped(tmp_path: Path) -> None:
    host = make_host(tmp_path)
    host.store.create_job("job", "project", 1)
    host.store.add_lead("job", "lead", LeadStatus.masked, in_sample=1)
    job_dir = tmp_path / "jobs" / "job"
    save_spec(
        JobSpec(
            1,
            "job",
            "project",
            "title",
            "question",
            LeadSourceSql("SELECT 1"),
            output_schema={"type": "object", "properties": {}},
            luna_model="gpt-5.6-luna",
            luna_effort="high",
        ),
        job_dir,
    )
    path = job_dir / "cache" / "transcripts"
    path.mkdir(parents=True)
    with gzip.open(path / "lead.jsonl.gz", "wt", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "call_id": "c1",
                    "call_start": "2026-01-01",
                    "transcript_text": "Call 9876543210\n" + ("x" * 50_000),
                }
            )
            + "\n"
        )
    raw_path = job_dir / "raw"
    raw_path.mkdir(parents=True)
    with gzip.open(raw_path / "lead.jsonl.gz", "wt", encoding="utf-8") as handle:
        handle.write(json.dumps({"transcript_text": "raw-only-secret"}) + "\n")
    result = host.call("lead_transcript", {"job_id": "job", "ref": "S1"})
    assert isinstance(result, str)
    assert "raw-only-secret" not in result
    assert "[PHONE]" in result
    assert "[..." in result


def test_write_file_allows_plan_and_summary_only(tmp_path: Path) -> None:
    host = make_host(tmp_path)
    assert host.call("write_file", {"name": "plan.md", "text": "plan"}) == "written"
    assert host.call("write_file", {"name": "summary.md", "text": "summary"}) == "written"
    assert (tmp_path / "plan.md").read_text(encoding="utf-8") == "plan"


def test_write_file_enforces_200k_cap(tmp_path: Path) -> None:
    with pytest.raises(ToolHostError, match="200k"):
        make_host(tmp_path).call("write_file", {"name": "plan.md", "text": "x" * 200_001})


def test_tool_errors_are_short_and_phone_like_numbers_are_masked(tmp_path: Path) -> None:
    host = make_host(tmp_path, FakeBqRunner(pages=[[{"number": 9876543210, "text": "9876543210"}]]))
    result = host.call("bq_query", {"sql": "SELECT 1"})
    rows = cast(list[dict[str, object]], cast(dict[str, object], result)["rows"])
    assert rows[0] == {"number": "[NUMBER]", "text": "[NUMBER]"}
    with pytest.raises(ToolHostError) as error:
        host.call("not_a_tool")
    assert "Traceback" not in str(error.value)
    assert str(tmp_path) not in str(error.value)


def test_tool_error_log_has_type_and_tool_without_exception_message(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "synthetic row data that must not be logged"
    host = make_host(tmp_path, FakeBqRunner(error=RuntimeError(secret)))
    caplog.set_level(logging.WARNING, logger="bca.mcp")

    with pytest.raises(ToolHostError, match="tool failed"):
        host.call("bq_query", {"sql": "SELECT 1"})

    assert "RuntimeError" in caplog.text
    assert "bq_query" in caplog.text
    assert secret not in caplog.text


@pytest.mark.parametrize(
    "function_name",
    [
        "TO_CODE_POINTS",
        "CODE_POINTS_TO_STRING",
        "CODE_POINTS_TO_BYTES",
        "TO_HEX",
        "FROM_HEX",
        "ASCII",
        "CHR",
        "UNICODE",
        "TO_BASE64",
        "FROM_BASE64",
        "TO_BASE32",
        "FROM_BASE32",
        "REVERSE",
    ],
)
def test_reconstruction_functions_are_rejected_but_string_literals_are_allowed(
    tmp_path: Path, function_name: str
) -> None:
    host = make_host(tmp_path)
    with pytest.raises(ToolHostError, match="reconstruct"):
        host.call("bq_query", {"sql": f"SELECT {function_name}(value)"})
    result = host.call("bq_query", {"sql": f"SELECT '{function_name}(value)'"})
    assert result["rows"] == []  # type: ignore[index]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT CAST(phone AS FLOAT64)",
        "SELECT CAST(phone AS INT64) AS lead_id",
        "SELECT TO_HEX/**/(value)",
        "SELECT SAFE.TO_HEX(value)",
        "SELECT 'a\\'', TO_HEX(value)",
        "SELECT TO_BASE64(value)",
        "SELECT FORMAT('%x', phone)",
        "SELECT REVERSE(email)",
    ],
)
def test_reconstruction_bypasses_are_rejected_or_masked(tmp_path: Path, sql: str) -> None:
    host = make_host(tmp_path, FakeBqRunner(pages=[[{"value": 9876543210.0, "lead_id": 9876543210}]]))
    if "FLOAT64" in sql or "INT64" in sql:
        result = host.call("bq_query", {"sql": "SELECT 1"})
        assert result["rows"][0] == {"value": "[NUMBER]", "lead_id": "[NUMBER]"}  # type: ignore[index]
    else:
        with pytest.raises(ToolHostError, match="reconstruct"):
            host.call("bq_query", {"sql": sql})


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1 # TO_HEX(value)",
        "SELECT '''TO_HEX(value)'''",
        "SELECT r'TO_HEX(value)'",
        "SELECT b'TO_HEX(value)'",
        "SELECT rb'TO_HEX(value)'",
    ],
)
def test_reconstruction_tokens_in_comments_and_literals_are_ignored(tmp_path: Path, sql: str) -> None:
    assert make_host(tmp_path).call("bq_query", {"sql": sql})["rows"] == []  # type: ignore[index]


def test_numeric_strings_have_no_column_exemptions(tmp_path: Path) -> None:
    runner = FakeBqRunner(
        pages=[[{"lead_id": "9876543210", "value": "012345678901"}]],
        table_details={("analytics", "events"): {"num_rows": 1234567890123, "schema": []}},
    )
    host = make_host(tmp_path, runner)
    result = host.call("bq_query", {"sql": "SELECT 1"})
    assert result["rows"][0] == {"lead_id": "[NUMBER]", "value": "[NUMBER]"}  # type: ignore[index]
    described = cast(
        dict[str, object], host.call("bq_describe_table", {"dataset": "analytics", "table": "events"})
    )
    assert described["num_rows"] == "[NUMBER]"


def test_data_notes_uses_configured_app_data_path(tmp_path: Path) -> None:
    notes = tmp_path / "app-data" / "notes.md"
    notes.parent.mkdir()
    notes.write_text("operator notes", encoding="utf-8")
    assert make_host(tmp_path / "project", notes_path=notes).call("data_notes") == "operator notes"


def proposal_json(title: str = "title", *, model: str = "gpt-5.6-luna") -> str:
    return json.dumps(
        {
            "spec_version": 1,
            "job_id": "job",
            "project_id": "project",
            "title": title,
            "question": "question",
            "lead_source": {"kind": "ids", "ids_file": "ids.txt", "count": 1},
            "calls_scope": "all",
            "usable": {},
            "cap": {},
            "instructions": "",
            "output_schema": {"type": "object", "properties": {}},
            "quote_fields": [],
            "rules": [],
            "luna_model": model,
            "luna_effort": "high",
            "sample_size": 1,
        }
    )


def proposal_host(
    tmp_path: Path,
    pages: list[list[dict[str, object]]],
    *,
    runner: FakeBqRunner | None = None,
) -> ToolHost:
    job_dir = tmp_path / "jobs" / "job"
    job_dir.mkdir(parents=True)
    (job_dir / "ids.txt").write_text("1001\n", encoding="utf-8")
    return make_host(
        tmp_path,
        runner or FakeBqRunner(pages=pages),
        recipe=recipe_config(),
    )


def test_propose_job_runs_preflight_before_plan_state_and_returns_card(tmp_path: Path) -> None:
    # propose_job runs lead-source dry runs/intake, then the preflight dry run/page fetch.
    runner = FakeBqRunner(
        pages_by_query_kind={
            "lead_source": [[{"lead_id": "1001"}]],
            "preflight": [
                [
                    {
                        "lead_id": "1001",
                        "found": True,
                        "calls": 1,
                        "call_id": "call-1",
                        "transcript_text": "synthetic transcript",
                        "duration": 600_000,
                        "transcript_calls": 1,
                        "usable_calls": 1,
                        "transcription_failed": 0,
                    }
                ]
            ],
        },
        dry=DryRun(0, [], [("lead_id", "STRING")]),
    )
    spec = json.loads(proposal_json())
    spec["lead_source"] = {"kind": "sql", "sql": "SELECT '1001' AS lead_id"}
    host = proposal_host(
        tmp_path,
        [],
        runner=runner,
    )
    result = host.call("propose_job", {"spec_json": json.dumps(spec)})
    card = cast(dict[str, object], result)
    preflight = cast(dict[str, object], card["preflight"])
    buckets = cast(dict[str, object], preflight["buckets"])
    assert card["state"] == "plan_proposed"
    assert buckets["usable"] == 1
    assert "per_lead" not in preflight
    assert card["lead_source_dry_run_bytes"] == "0 B"
    assert [tuple(row) for row in host.store.rows("SELECT state, spec_version FROM jobs WHERE id='job'")] == [
        ("plan_proposed", 1)
    ]


def test_propose_job_plan_card_stays_structured_for_thousands_of_leads(tmp_path: Path) -> None:
    lead_ids = [str(index) for index in range(1, 3001)]
    rows: list[dict[str, object]] = [
        {
            "lead_id": lead_id,
            "found": True,
            "calls": 1,
            "call_id": f"call-{lead_id}",
            "transcript_text": "synthetic transcript",
            "duration": 600_000,
            "transcript_calls": 1,
            "usable_calls": 1,
            "transcription_failed": 0,
        }
        for lead_id in lead_ids
    ]
    runner = FakeBqRunner(pages=[rows], dry=DryRun(0, [], [("lead_id", "STRING")]))
    host = proposal_host(tmp_path, [], runner=runner)
    (tmp_path / "jobs" / "job" / "ids.txt").write_text("\n".join(lead_ids), encoding="utf-8")
    card = cast(dict[str, object], host.call("propose_job", {"spec_json": proposal_json()}))
    assert isinstance(card["lead_counts"], dict)
    assert isinstance(card["preflight"], dict)
    assert card["lead_source_dry_run_bytes"] == "0 B"
    assert card["spec_version"] == 1


def test_propose_job_reproposal_versions_and_running_job_is_refused(tmp_path: Path) -> None:
    pages: list[list[dict[str, object]]] = [
        [
            {
                "lead_id": lead_id,
                "found": True,
                "calls": 1,
                "call_id": f"call-{lead_id}",
                "transcript_text": "synthetic transcript",
                "duration": 600_000,
                "transcript_calls": 1,
                "usable_calls": 1,
                "transcription_failed": 0,
            }
            for lead_id in ("1001", "2001")
        ]
    ]
    host = proposal_host(
        tmp_path,
        pages,
    )
    host.call("propose_job", {"spec_json": proposal_json()})
    (tmp_path / "jobs" / "job" / "ids.txt").write_text("2001\n", encoding="utf-8")
    result = host.call("propose_job", {"spec_json": proposal_json("revised")})
    assert result["spec"] == "spec.v2.json"  # type: ignore[index]
    rows = host.store.rows("SELECT state, spec_version FROM jobs WHERE id='job'")
    assert [tuple(row) for row in rows] == [("plan_proposed", 2)]
    assert [tuple(row) for row in host.store.rows("SELECT lead_id FROM leads WHERE job_id='job'")] == [
        ("2001",)
    ]
    with host.store.transaction() as cur:
        cur.execute("UPDATE jobs SET state='full_running' WHERE id='job'")
    with pytest.raises(ToolHostError, match="full run starts"):
        host.call("propose_job", {"spec_json": proposal_json("blocked")})


def test_propose_job_preflight_failure_rolls_back_new_job_and_spec(tmp_path: Path) -> None:
    host = proposal_host(tmp_path, [], runner=FakeBqRunner(error=ValueError("preflight failed")))
    with pytest.raises(ToolHostError, match="preflight"):
        host.call("propose_job", {"spec_json": proposal_json()})
    assert host.store.rows("SELECT id FROM jobs WHERE id='job'") == []
    assert not (tmp_path / "jobs" / "job" / "spec.v1.json").exists()


def test_propose_job_rejects_model_selected_by_caller(tmp_path: Path) -> None:
    host = proposal_host(tmp_path, [])
    with pytest.raises(ToolHostError, match="configured models"):
        host.call("propose_job", {"spec_json": proposal_json(model="caller-picked")})
