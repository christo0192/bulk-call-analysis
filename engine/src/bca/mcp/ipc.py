"""Local IPC helpers.

IPC security: D-PIPE deliberately uses the standard Windows named-pipe
listener.  Its default DACL grants Everyone read access, but not write
access, so another user cannot open the pipe for a useful connection or
complete the HMAC handshake.  Handshake deadlines also prevent a read-only
opener from wedging the listener.
"""

from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Mapping
from multiprocessing.connection import Client
from pathlib import Path

from .host import ToolHostError


def ipc_family() -> str:
    return "AF_PIPE" if sys.platform == "win32" else "AF_UNIX"


def new_ipc_address() -> tuple[str, str]:
    """Return a private local IPC endpoint and its multiprocessing family."""
    if sys.platform == "win32":
        return rf"\\.\pipe\bca-{os.urandom(16).hex()}", "AF_PIPE"
    directory = Path(tempfile.mkdtemp(prefix="bca-ipc-"))
    directory.chmod(0o700)
    return str(directory / "engine.sock"), "AF_UNIX"


def call_engine(
    address: str,
    authkey: bytes,
    tool: str,
    arguments: Mapping[str, object],
    family: str | None = None,
    project_id: str | None = None,
) -> object:
    connection = Client(address, family=family or ipc_family(), authkey=authkey)
    try:
        request: dict[str, object] = {"tool": tool, "arguments": dict(arguments)}
        if project_id is not None:
            request["project_id"] = project_id
        connection.send(request)
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
