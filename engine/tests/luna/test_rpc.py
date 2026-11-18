import asyncio
import json

import pytest

from bca.luna.rpc import FORBIDDEN_METHODS, AppServerClient, ForbiddenMethodError, RpcError, RpcTimeout


class Writer:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.messages.append(json.loads(data))

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return


def make_client() -> tuple[asyncio.StreamReader, Writer, AppServerClient]:
    reader = asyncio.StreamReader()
    writer = Writer()
    return reader, writer, AppServerClient(reader, writer)  # type: ignore[arg-type]


def test_request_response_and_rpc_error() -> None:
    asyncio.run(_test_request_response_and_rpc_error())


async def _test_request_response_and_rpc_error() -> None:
    reader, writer, client = make_client()
    request = asyncio.create_task(client.request("ping", {"x": 1}))
    await asyncio.sleep(0)
    reader.feed_data(b'{"id":1,"result":{"ok":true}}\n')
    assert await request == {"ok": True}
    failed = asyncio.create_task(client.request("bad"))
    await asyncio.sleep(0)
    reader.feed_data(b'{"id":2,"error":{"code":-8,"message":"no"}}\n')
    with pytest.raises(RpcError) as exc:
        await failed
    assert exc.value.code == -8
    await client.close()
    assert writer.closed


def test_timeout_notifications_and_server_requests() -> None:
    asyncio.run(_test_timeout_notifications_and_server_requests())


async def _test_timeout_notifications_and_server_requests() -> None:
    reader, writer, client = make_client()
    with pytest.raises(RpcTimeout):
        await client.request("slow", timeout_s=0.001)
    seen: list[dict[str, object]] = []
    client.subscribe("turn/", lambda message: seen.append(message))
    client.subscribe("turn/completed", lambda message: seen.append(message))
    reader.feed_data(b"not json\n")
    reader.feed_data(b'{"method":"turn/completed","params":{}}\n')
    reader.feed_data(b'{"id":7,"method":"item/commandExecution/requestApproval"}\n')
    await asyncio.sleep(0.01)
    assert len(seen) == 2 and client.malformed_lines == 1
    assert writer.messages[-1] == {"id": 7, "result": {"decision": "cancel"}}
    await client.close()


def test_unknown_server_request_and_forbidden_methods() -> None:
    asyncio.run(_test_unknown_server_request_and_forbidden_methods())


async def _test_unknown_server_request_and_forbidden_methods() -> None:
    reader, writer, client = make_client()
    reader.feed_data(b'{"id":4,"method":"unknown"}\n')
    await asyncio.sleep(0.01)
    assert writer.messages == [
        {"id": 4, "error": {"code": -32601, "message": "server request not supported"}}
    ]
    for method in FORBIDDEN_METHODS:
        with pytest.raises(ForbiddenMethodError):
            await client.request(method)
    assert len(writer.messages) == 1
    await client.close()
