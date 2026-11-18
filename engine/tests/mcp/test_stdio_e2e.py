from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import TextContent

from bca.mcp.host import start_ipc_thread
from bca.mcp.ipc import new_ipc_address
from tests.fakes.fake_bq import FakeBqRunner
from tests.mcp.test_host import make_host


def test_bq_stdio_shim_round_trips_to_host(tmp_path: Path) -> None:
    host = make_host(tmp_path, FakeBqRunner(pages=[[{"phone": "Call 9876543210", "count": 7}]]))
    address, _family = new_ipc_address()
    authkey = b"e" * 32
    thread = start_ipc_thread(host, address, authkey)
    environment = os.environ.copy()
    environment["BCA_MCP_AUTHKEY"] = authkey.hex()
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "bca.mcp.bq_server", "--engine-socket", address, "--project", "project"],
        env=environment,
    )
    assert authkey.hex() not in parameters.args

    async def exercise() -> None:
        async with (
            stdio_client(parameters) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()
            tools = await session.list_tools()
            assert {tool.name for tool in tools.tools} == {
                "bq_list_datasets",
                "bq_list_tables",
                "bq_describe_table",
                "bq_dry_run",
                "bq_query",
                "bq_information_schema",
            }
            result = await session.call_tool("bq_query", {"sql": "SELECT 1"})
            assert not result.is_error
            assert isinstance(result.content[0], TextContent)
            payload = json.loads(result.content[0].text)
            assert payload["rows"] == [{"phone": "Call [PHONE]", "count": 7}]

    try:
        asyncio.run(exercise())
    finally:
        host.close()
        assert not thread.is_alive()
