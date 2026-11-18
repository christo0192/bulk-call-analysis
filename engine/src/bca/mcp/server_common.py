from __future__ import annotations

import argparse
import os
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

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


def _call(address: str, tool: str, arguments: dict[str, object]) -> object:
    try:
        return call_engine(address, _authkey(), tool, arguments)
    except Exception as exc:
        raise ToolError(str(exc)) from None


def _typed(function: Any, annotations: dict[str, object]) -> Any:
    function.__annotations__ = annotations
    return function


def build_server(name: str, address: str, tools: tuple[str, ...]) -> MCPServer:
    server = MCPServer(name=name)
    handlers: dict[str, Any] = {
        "bq_list_datasets": lambda: _call(address, "bq_list_datasets", {}),
        "bq_list_tables": lambda dataset: _call(address, "bq_list_tables", {"dataset": dataset}),
        "bq_describe_table": lambda dataset, table: _call(
            address, "bq_describe_table", {"dataset": dataset, "table": table}
        ),
        "bq_dry_run": lambda sql: _call(address, "bq_dry_run", {"sql": sql}),
        "bq_query": lambda sql, max_rows=200: _call(address, "bq_query", {"sql": sql, "max_rows": max_rows}),
        "bq_information_schema": lambda dataset, view: _call(
            address, "bq_information_schema", {"dataset": dataset, "view": view}
        ),
        "data_notes": lambda: _call(address, "data_notes", {}),
        "brief_read": lambda: _call(address, "brief_read", {}),
        "brief_update": lambda text: _call(address, "brief_update", {"text": text}),
        "propose_job": lambda spec_json: _call(address, "propose_job", {"spec_json": spec_json}),
        "job_status": lambda job_id: _call(address, "job_status", {"job_id": job_id}),
        "sample_results": lambda job_id, offset=0, limit=20: _call(
            address, "sample_results", {"job_id": job_id, "offset": offset, "limit": limit}
        ),
        "lead_transcript": lambda job_id, lead_id, call_from=1, call_to=3: _call(
            address,
            "lead_transcript",
            {"job_id": job_id, "lead_id": lead_id, "call_from": call_from, "call_to": call_to},
        ),
        "result_aggregates": lambda job_id, group_by=None: _call(
            address, "result_aggregates", {"job_id": job_id, "group_by": group_by}
        ),
        "write_file": lambda name, text: _call(address, "write_file", {"name": name, "text": text}),
    }
    annotations: dict[str, dict[str, object]] = {
        "bq_list_datasets": {"return": object},
        "bq_list_tables": {"dataset": str, "return": object},
        "bq_describe_table": {"dataset": str, "table": str, "return": object},
        "bq_dry_run": {"sql": str, "return": object},
        "bq_query": {"sql": str, "max_rows": int, "return": object},
        "bq_information_schema": {"dataset": str, "view": str, "return": object},
        "data_notes": {"return": str},
        "brief_read": {"return": str},
        "brief_update": {"text": str, "return": str},
        "propose_job": {"spec_json": str, "return": object},
        "job_status": {"job_id": str, "return": object},
        "sample_results": {"job_id": str, "offset": int, "limit": int, "return": object},
        "lead_transcript": {"job_id": str, "lead_id": str, "call_from": int, "call_to": int, "return": str},
        "result_aggregates": {"job_id": str, "group_by": list[str] | None, "return": object},
        "write_file": {"name": str, "text": str, "return": str},
    }
    for tool_name in tools:
        server.add_tool(_typed(handlers[tool_name], annotations[tool_name]), name=tool_name)
    return server


def run_shim(name: str, tools: tuple[str, ...]) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine-socket", required=True)
    parser.add_argument("--project", required=True)
    args = parser.parse_args()
    build_server(name, args.engine_socket, tools).run("stdio")
