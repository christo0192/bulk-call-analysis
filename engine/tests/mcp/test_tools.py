from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

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


def test_bq_dry_run_through_host(tmp_path: Path) -> None:
    result = make_host(tmp_path, FakeBqRunner(dry=DryRun(12, ["events"], [("id", "INT64")]))).call(
        "bq_dry_run", {"sql": "SELECT 1"}
    )
    assert result == {"bytes": 12, "referenced_tables": ["events"], "schema": [["id", "INT64"]]}


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
    host.store.add_lead("job", "lead", LeadStatus.done, in_sample=1)
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


def test_lead_transcript_is_masked_and_capped(tmp_path: Path) -> None:
    host = make_host(tmp_path)
    host.store.create_job("job", "project", 1)
    host.store.add_lead("job", "lead", LeadStatus.done, in_sample=1)
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
    path = job_dir / "raw"
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
    result = host.call("lead_transcript", {"job_id": "job", "lead_id": "lead"})
    assert isinstance(result, str)
    assert "[PHONE]" in result
    assert "[truncated:" in result


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
    assert result["rows"][0] == {"number": "[NUMBER]", "text": "[PHONE]"}  # type: ignore[index]
    with pytest.raises(ToolHostError) as error:
        host.call("not_a_tool")
    assert "Traceback" not in str(error.value)
    assert str(tmp_path) not in str(error.value)


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
            "lead_source": {"kind": "ids", "ids_file": "ids.txt"},
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


def proposal_host(tmp_path: Path, pages: list[list[dict[str, object]]]) -> ToolHost:
    job_dir = tmp_path / "jobs" / "job"
    job_dir.mkdir(parents=True)
    (job_dir / "ids.txt").write_text("lead-1\n", encoding="utf-8")
    return make_host(
        tmp_path,
        FakeBqRunner(pages=pages),
        recipe=recipe_config(),
    )


def test_propose_job_runs_preflight_before_plan_state_and_returns_card(tmp_path: Path) -> None:
    host = proposal_host(
        tmp_path,
        [
            [
                {
                    "lead_id": "lead-1",
                    "found": True,
                    "calls": 1,
                    "transcript_calls": 1,
                    "usable_calls": 1,
                    "transcription_failed": 0,
                }
            ]
        ],
    )
    result = host.call("propose_job", {"spec_json": proposal_json()})
    assert result["state"] == "plan_proposed"  # type: ignore[index]
    assert result["preflight"]["buckets"]["usable"] == 1  # type: ignore[index]
    assert result["lead_source_dry_run_bytes"] == 0  # type: ignore[index]
    assert host.store.rows("SELECT state, spec_version FROM jobs WHERE id='job'") == [("plan_proposed", 1)]


def test_propose_job_reproposal_versions_and_running_job_is_refused(tmp_path: Path) -> None:
    host = proposal_host(
        tmp_path,
        [
            [
                {
                    "lead_id": "lead-1",
                    "found": True,
                    "calls": 1,
                    "transcript_calls": 1,
                    "usable_calls": 1,
                    "transcription_failed": 0,
                }
            ]
        ],
    )
    host.call("propose_job", {"spec_json": proposal_json()})
    result = host.call("propose_job", {"spec_json": proposal_json("revised")})
    assert result["spec"] == "spec.v2.json"  # type: ignore[index]
    assert host.store.rows("SELECT state, spec_version FROM jobs WHERE id='job'") == [("plan_proposed", 2)]
    with host.store.transaction() as cur:
        cur.execute("UPDATE jobs SET state='full_running' WHERE id='job'")
    with pytest.raises(ToolHostError, match="full run starts"):
        host.call("propose_job", {"spec_json": proposal_json("blocked")})


def test_propose_job_preflight_failure_rolls_back_new_job_and_spec(tmp_path: Path) -> None:
    host = proposal_host(tmp_path, [[{"lead_id": "other", "found": True}]])
    with pytest.raises(ToolHostError, match="preflight"):
        host.call("propose_job", {"spec_json": proposal_json()})
    assert host.store.rows("SELECT id FROM jobs WHERE id='job'") == []
    assert not (tmp_path / "jobs" / "job" / "spec.v1.json").exists()


def test_propose_job_rejects_model_selected_by_caller(tmp_path: Path) -> None:
    host = proposal_host(tmp_path, [])
    with pytest.raises(ToolHostError, match="configured models"):
        host.call("propose_job", {"spec_json": proposal_json(model="caller-picked")})
