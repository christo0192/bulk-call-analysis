from __future__ import annotations

from collections.abc import Mapping
from multiprocessing.connection import Client

from .host import ToolHostError


def call_engine(address: str, authkey: bytes, tool: str, arguments: Mapping[str, object]) -> object:
    connection = Client(address, authkey=authkey)
    try:
        connection.send({"tool": tool, "arguments": dict(arguments)})
        response = connection.recv()
        if not isinstance(response, dict) or not response.get("ok"):
            reason = (
                response.get("error", "engine tool failed")
                if isinstance(response, dict)
                else "engine tool failed"
            )
            raise ToolHostError(str(reason))
        return response.get("result")
    finally:
        connection.close()
