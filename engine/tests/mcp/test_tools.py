from __future__ import annotations

from pathlib import Path

import pytest

from bca.bq.runner import DryRun
from bca.mcp.host import ToolHostError
from bca.store import LeadStatus
from tests.fakes.fake_bq import FakeBqRunner
from tests.mcp.test_host import make_host


class MetadataRunner(FakeBqRunner):
    def list_datasets(self) -> list[dict[str, object]]:
        return [{"dataset_id": "analytics"}]

    def list_tables(self, dataset: str) -> list[dict[str, object]]:
        return [{"table_id": "events", "dataset": dataset}]

    def describe_table(self, dataset: str, table: str) -> dict[str, object]:
        return {"dataset": dataset, "table": table, "schema": []}


def test_bq_list_datasets_through_host(tmp_path: Path) -> None:
    assert make_host(tmp_path, MetadataRunner()).call("bq_list_datasets") == [{"dataset_id": "analytics"}]


def test_bq_list_tables_through_host(tmp_path: Path) -> None:
    assert make_host(tmp_path, MetadataRunner()).call("bq_list_tables", {"dataset": "analytics"}) == [
        {"table_id": "events", "dataset": "analytics"}
    ]


def test_bq_list_tables_rejects_invalid_identifier(tmp_path: Path) -> None:
    with pytest.raises(ToolHostError, match="invalid dataset"):
        make_host(tmp_path, MetadataRunner()).call("bq_list_tables", {"dataset": "bad-name"})


def test_bq_describe_table_through_host(tmp_path: Path) -> None:
    result = make_host(tmp_path, MetadataRunner()).call(
        "bq_describe_table", {"dataset": "analytics", "table": "events"}
    )
    assert result == {"dataset": "analytics", "table": "events", "schema": []}


def test_bq_dry_run_through_host(tmp_path: Path) -> None:
    result = make_host(tmp_path, MetadataRunner(dry=DryRun(12, ["events"], [("id", "INT64")]))).call(
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


def test_sample_results_excludes_non_sample_leads(tmp_path: Path) -> None:
    host = make_host(tmp_path)
    host.store.create_job("job", "project", 1)
    host.store.add_lead("job", "lead", LeadStatus.done, in_sample=0)
    assert host.call("sample_results", {"job_id": "job"}) == []


def test_lead_transcript_is_masked_and_capped(tmp_path: Path) -> None:
    host = make_host(tmp_path)
    host.store.create_job("job", "project", 1)
    host.store.add_lead("job", "lead", LeadStatus.done, in_sample=1)
    path = tmp_path / "jobs" / "job" / "masked"
    path.mkdir(parents=True)
    (path / "lead.txt").write_text("=== CALL 1\nCall 9876543210\n" + ("x" * 50_000), encoding="utf-8")
    result = host.call("lead_transcript", {"job_id": "job", "lead_id": "lead"})
    assert "[PHONE]" in result["value"]  # type: ignore[index]
    assert "characters omitted" in result["truncated"]  # type: ignore[index]


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
