from __future__ import annotations

import asyncio

from bca.mcp.server_common import TOOLS_APP, TOOLS_BQ, build_server


def test_tool_lists_are_exact_and_have_no_control_tools() -> None:
    async def check() -> None:
        bq = await build_server("bq", "unused", TOOLS_BQ).list_tools()
        app = await build_server("app", "unused", TOOLS_APP).list_tools()
        assert {tool.name for tool in bq} == set(TOOLS_BQ)
        assert {tool.name for tool in app} == set(TOOLS_APP)
        assert not {tool.name for tool in [*bq, *app]} & {"approve", "start_full_run", "start"}

    asyncio.run(check())
