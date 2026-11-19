from __future__ import annotations

import argparse
import os
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from .ipc import call_engine

TOOLS_BQ = (
    "bq_list_datasets",
    "bq_list_tables",
    "bq_describe_table",
    "bq_dry_run",
    "bq_query",
    "bq_information_schema",
)
TOOLS_APP = (
    "data_notes",
    "brief_read",
    "brief_update",
    "propose_job",
    "job_status",
    "sample_results",
    "lead_transcript",
    "result_aggregates",
    "write_file",
)


def _authkey() -> bytes:
    value = os.environ.get("BCA_MCP_AUTHKEY")
    if not value:
        raise RuntimeError("BCA_MCP_AUTHKEY is not set")
    return bytes.fromhex(value)


def _call(address: str, tool: str, arguments: dict[str, object], project_id: str) -> object:
    try:
        return call_engine(address, _authkey(), tool, arguments, project_id=project_id)
    except Exception as exc:
        raise ToolError(str(exc)) from None


def _typed(function: Any, annotations: dict[str, object]) -> Any:
    function.__annotations__ = annotations
    return function


def _arg(kind: object, description: str) -> object:
    return Annotated[kind, Field(description=description)]


def build_server(name: str, address: str, tools: tuple[str, ...], project_id: str = "project") -> MCPServer:
    server = MCPServer(name=name)

    def invoke(tool: str, arguments: dict[str, object]) -> object:
        return _call(address, tool, arguments, project_id)

    handlers: dict[str, Any] = {
        "bq_list_datasets": lambda: invoke("bq_list_datasets", {}),
        "bq_list_tables": lambda dataset: invoke("bq_list_tables", {"dataset": dataset}),
        "bq_describe_table": lambda dataset, table: invoke(
            "bq_describe_table", {"dataset": dataset, "table": table}
        ),
        "bq_dry_run": lambda sql: invoke("bq_dry_run", {"sql": sql}),
        "bq_query": lambda sql, max_rows=200: invoke("bq_query", {"sql": sql, "max_rows": max_rows}),
        "bq_information_schema": lambda dataset, view: invoke(
            "bq_information_schema", {"dataset": dataset, "view": view}
        ),
        "data_notes": lambda: invoke("data_notes", {}),
        "brief_read": lambda: invoke("brief_read", {}),
        "brief_update": lambda text: invoke("brief_update", {"text": text}),
        "propose_job": lambda spec_json: invoke("propose_job", {"spec_json": spec_json}),
        "job_status": lambda job_id: invoke("job_status", {"job_id": job_id}),
        "sample_results": lambda job_id, offset=0, limit=20: invoke(
            "sample_results", {"job_id": job_id, "offset": offset, "limit": limit}
        ),
        "lead_transcript": lambda job_id, ref, call_from=1, call_to=3: invoke(
            "lead_transcript",
            {"job_id": job_id, "ref": ref, "call_from": call_from, "call_to": call_to},
        ),
        "result_aggregates": lambda job_id, group_by=None: invoke(
            "result_aggregates", {"job_id": job_id, "group_by": group_by}
        ),
        "write_file": lambda name, text: invoke("write_file", {"name": name, "text": text}),
    }
    annotations: dict[str, dict[str, object]] = {
        "bq_list_datasets": {"return": object},
        "bq_list_tables": {"dataset": _arg(str, "Dataset identifier."), "return": object},
        "bq_describe_table": {
            "dataset": _arg(str, "Dataset identifier."),
            "table": _arg(str, "Table identifier."),
            "return": object,
        },
        "bq_dry_run": {"sql": _arg(str, "A read-only SELECT statement."), "return": object},
        "bq_query": {
            "sql": _arg(str, "A read-only SELECT; reconstruction functions are refused."),
            "max_rows": _arg(int, "Rows to return, from 1 through 1000."),
            "return": object,
        },
        "bq_information_schema": {
            "dataset": _arg(str, "Dataset identifier."),
            "view": _arg(str, "Allowlisted INFORMATION_SCHEMA view."),
            "return": object,
        },
        "data_notes": {"return": str},
        "brief_read": {"return": str},
        "brief_update": {"text": _arg(str, "Brief text, at most 20k characters."), "return": str},
        "propose_job": {
            "spec_json": _arg(
                str,
                "JSON job spec with job_id, lead_source, rules, output_schema, and question; "
                "configured model values are required.",
            ),
            "return": object,
        },
        "job_status": {"job_id": _arg(str, "Job identifier."), "return": object},
        "sample_results": {
            "job_id": _arg(str, "Job identifier."),
            "offset": _arg(int, "Zero-based page offset."),
            "limit": _arg(int, "Page size, at most 100."),
            "return": object,
        },
        "lead_transcript": {
            "job_id": _arg(str, "Job identifier."),
            "ref": _arg(str, "Opaque sample reference returned by sample_results, such as S1."),
            "call_from": _arg(int, "First one-based call number."),
            "call_to": _arg(int, "Last one-based call number."),
            "return": str,
        },
        "result_aggregates": {
            "job_id": _arg(str, "Job identifier."),
            "group_by": _arg(list[str] | None, "Optional output fields to group by."),
            "return": object,
        },
        "write_file": {
            "name": _arg(str, "Only plan.md or summary.md."),
            "text": _arg(str, "Text, at most 200k characters."),
            "return": str,
        },
    }
    descriptions = {
        "bq_list_datasets": "List datasets; read-only and masked.",
        "bq_list_tables": "List tables in a dataset; dataset must be an identifier.",
        "bq_describe_table": "Describe schema and row count; read-only.",
        "bq_dry_run": (
            "Validate a SELECT and return its estimated scan size as a readable string without executing it."
        ),
        "bq_query": (
            "Run a masked SELECT of at most 1000 rows; report scan size as a readable string "
            "and refuse scans above reject_bytes."
        ),
        "bq_information_schema": "Read an allowlisted INFORMATION_SCHEMA view.",
        "data_notes": "Read operator notes from the configured app-data notes file.",
        "brief_read": "Read the project brief.",
        "brief_update": "Write a brief of at most 20k characters.",
        "propose_job": (
            "Propose a validated job spec_json object; models come from configuration "
            "and pre-flight is required."
        ),
        "job_status": "Return progress counts, checklist, ETA, quota state, and recent errors.",
        "sample_results": (
            "Read a bounded page of masked sample results identified by opaque refs such as S1."
        ),
        "lead_transcript": "Read only leak-scanned masked transcript calls for an opaque sample reference.",
        "result_aggregates": "Read masked result aggregates.",
        "write_file": "Write only plan.md or summary.md, with a 200k character cap.",
    }
    for tool_name in tools:
        server.add_tool(
            _typed(handlers[tool_name], annotations[tool_name]),
            name=tool_name,
            description=descriptions[tool_name],
        )
    return server


def run_shim(name: str, tools: tuple[str, ...]) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine-socket", required=True)
    parser.add_argument("--project", required=True)
    args = parser.parse_args()
    build_server(name, args.engine_socket, tools, args.project).run("stdio")
