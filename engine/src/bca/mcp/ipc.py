"""Local IPC helpers.

IPC security: D-PIPE deliberately uses the standard Windows named-pipe
listener.  The explicit BCA1 HMAC handshake and deadlines prevent a peer from
using multiprocessing's pickled authentication protocol to wedge the listener.
"""

from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Mapping
from multiprocessing.connection import Client
from pathlib import Path

from .host import ToolHostError
from .wire import (
    CALL_TIMEOUT,
    HELLO_PREFIX,
    MAX_REPLY_BYTES,
    OK,
    decode_json,
    encode_json,
    expected_mac,
    ipc_family,
)


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
    try:
        connection = Client(address, family=family or ipc_family(), authkey=None)
    except OSError as exc:
        raise ToolHostError("engine is unavailable") from exc
    try:
        request: dict[str, object] = {"tool": tool, "arguments": dict(arguments)}
        if project_id is not None:
            request["project_id"] = project_id
        if not connection.poll(5.0):
            raise ToolHostError("engine did not answer")
        hello = connection.recv_bytes(64)
        if len(hello) != 36 or hello[:4] != HELLO_PREFIX:
            raise ToolHostError("engine rejected the connection")
        connection.send_bytes(expected_mac(authkey, hello))
        if not connection.poll(5.0) or connection.recv_bytes(8) != OK:
            raise ToolHostError("engine rejected the connection")
        connection.send_bytes(encode_json(request))
        if not connection.poll(CALL_TIMEOUT):
            raise ToolHostError("engine timed out")
        response = decode_json(connection.recv_bytes(MAX_REPLY_BYTES))
        if not isinstance(response, dict) or not response.get("ok"):
            reason = (
                response.get("error", "engine tool failed")
                if isinstance(response, dict)
                else "engine tool failed"
            )
            raise ToolHostError(str(reason))
        return response.get("result")
    except (OSError, EOFError, ValueError, UnicodeError) as exc:
        raise ToolHostError("engine rejected the connection") from exc
    finally:
        connection.close()
