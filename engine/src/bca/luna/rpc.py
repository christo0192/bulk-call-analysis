from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

log = logging.getLogger(__name__)
JsonObject = dict[str, Any]
Callback = Callable[[JsonObject], Awaitable[None] | None]
FORBIDDEN_METHODS = frozenset(
    {
        "account/rateLimitResetCredit/consume",
        "account/sendAddCreditsNudgeEmail",
        "account/login/start",
        "account/logout",
    }
)


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"RPC error {code}: {message}")
        self.code, self.message, self.data = code, message, data


class RpcTimeout(TimeoutError):
    pass


class ForbiddenMethodError(ValueError):
    pass


class AppServerClient:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader, self.writer = reader, writer
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[JsonObject]] = {}
        self._subscriptions: list[tuple[str, Callback]] = []
        self._any: list[Callback] = []
        self.malformed_lines = 0
        self._closed = False
        self._reader_task = asyncio.create_task(self._read_loop())

    async def request(
        self, method: str, params: Mapping[str, Any] | None = None, timeout_s: float = 30
    ) -> JsonObject:
        if method in FORBIDDEN_METHODS or "apiKey" in method:
            raise ForbiddenMethodError(f"forbidden outgoing method: {method}")
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[JsonObject] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        message: JsonObject = {"id": request_id, "method": method, "params": dict(params or {})}
        try:
            self._write(message)
            try:
                response = await asyncio.wait_for(future, timeout_s)
            except TimeoutError as exc:
                raise RpcTimeout(f"timed out waiting for {method}") from exc
            if "error" in response:
                error = response["error"]
                raise RpcError(
                    int(error.get("code", -1)), str(error.get("message", "unknown error")), error.get("data")
                )
            result = response.get("result", {})
            return result if isinstance(result, dict) else {"value": result}
        finally:
            self._pending.pop(request_id, None)

    def subscribe(self, method_or_prefix: str, callback: Callback) -> None:
        self._subscriptions.append((method_or_prefix, callback))

    def on_any_notification(self, callback: Callback) -> None:
        self._any.append(callback)

    def _write(self, message: JsonObject) -> None:
        self.writer.write((json.dumps(message, separators=(",", ":")) + "\n").encode())

    async def _read_loop(self) -> None:
        try:
            while not self.reader.at_eof():
                line = await self.reader.readline()
                if not line:
                    break
                try:
                    message = json.loads(line)
                    if not isinstance(message, dict):
                        raise ValueError("message is not an object")
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                    self.malformed_lines += 1
                    log.warning("malformed app-server line: %s", exc)
                    continue
                try:
                    await self._dispatch(message)
                except Exception:
                    log.exception("app-server notification callback failed")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("app-server reader failed")
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("app-server connection closed"))

    async def _dispatch(self, message: JsonObject) -> None:
        if "id" in message and ("result" in message or "error" in message):
            request_id = message["id"]
            if isinstance(request_id, int) and request_id in self._pending:
                self._pending[request_id].set_result(message)
            return
        method = message.get("method")
        if not isinstance(method, str):
            return
        if "id" in message:
            log.warning("declining server request %s", method)
            response: JsonObject
            if method.endswith("commandExecution/requestApproval") or method.endswith(
                "fileChange/requestApproval"
            ):
                response = {"result": {"decision": "cancel"}}
            elif method.endswith("permissions/requestApproval"):
                response = {"result": {"permissions": {}}}
            elif method == "execCommandApproval" or method == "applyPatchApproval":
                response = {"result": {"denied": {"reason": "declined by policy"}}}
            else:
                response = {"error": {"code": -32601, "message": "server request not supported"}}
            self._write({"id": message["id"], **response})
            for prefix, callback in [*self._subscriptions, *[("", cb) for cb in self._any]]:
                if prefix and not method.startswith(prefix):
                    continue
                result = callback(message)
                if inspect.isawaitable(result):
                    await result
            return
        for prefix, callback in [*self._subscriptions, *[("", cb) for cb in self._any]]:
            if prefix and not method.startswith(prefix):
                continue
            result = callback(message)
            if inspect.isawaitable(result):
                await result

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._reader_task.cancel()
        await asyncio.gather(self._reader_task, return_exceptions=True)
        self.writer.close()
        await self.writer.wait_closed()
