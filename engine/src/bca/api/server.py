from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import sys
import threading
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO, cast

from bca.monitor.events import Event, EventBus
from bca.store.lifecycle import public_state

Json = dict[str, object]
Notify = Callable[[Json], Awaitable[None]]


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: object | None = None) -> None:
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


def strict_json_value(value: object, logger: logging.Logger) -> object:
    if isinstance(value, float) and not math.isfinite(value):
        logger.debug("replacing non-finite JSON number with null")
        return None
    if isinstance(value, dict):
        return {key: strict_json_value(item, logger) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [strict_json_value(item, logger) for item in value]
    return value


def _object(value: object, name: str = "params") -> dict[str, object]:
    if not isinstance(value, dict):
        raise RpcError(-32602, "Invalid params", {"field": name, "reason": "must be an object"})
    return cast(dict[str, object], value)


def _required(data: Mapping[str, object], key: str, typ: type[object]) -> object:
    value = data.get(key)
    if not isinstance(value, typ):
        raise RpcError(-32602, "Invalid params", {"field": key, "reason": "required"})
    return value


def _string(data: Mapping[str, object], key: str) -> str:
    value = _required(data, key, str)
    if not value:
        raise RpcError(-32602, "Invalid params", {"field": key, "reason": "must not be empty"})
    return cast(str, value)


def _int(data: Mapping[str, object], key: str) -> int:
    value = _required(data, key, int)
    if isinstance(value, bool):
        raise RpcError(-32602, "Invalid params", {"field": key, "reason": "must be an integer"})
    return cast(int, value)


def _exact(data: dict[str, object], allowed: set[str]) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise RpcError(-32602, "Invalid params", {"fields": unknown, "reason": "unknown field"})


class ApiBackend:
    """Small integration seam for the coordinator and application services.

    Deployments may implement only the methods they support; the service reports
    unsupported operations as a normal JSON-RPC error rather than leaking an
    AttributeError or an implementation detail.
    """

    async def call(self, method: str, params: dict[str, object]) -> object:
        raise NotImplementedError(method)


@dataclass
class MemoryBackend(ApiBackend):
    """Deterministic, non-network backend used by the standalone shell and tests."""

    projects: dict[str, dict[str, object]] = field(default_factory=dict)
    settings: dict[str, object] = field(default_factory=dict)
    jobs: dict[str, dict[str, object]] = field(default_factory=dict)
    auth: dict[str, bool] = field(default_factory=lambda: {"claude": False, "codex": False})

    async def call(self, method: str, params: dict[str, object]) -> object:
        if method == "projects/list":
            return list(self.projects.values())
        if method == "projects/jobs":
            project = _string(params, "project")
            if project not in self.projects:
                raise RpcError(-32001, "Project not found", {"reason": "project_not_found"})
            return {"jobs": []}
        if method == "projects/create":
            project_id = _string(params, "project")
            title = _string(params, "title")
            if project_id in self.projects:
                raise RpcError(-32001, "Project already exists", {"project": project_id})
            self.projects[project_id] = {"project": project_id, "title": title}
            return self.projects[project_id]
        if method == "files/list":
            _string(params, "project")
            return []
        if method == "files/path":
            _string(params, "project")
            _string(params, "name")
            raise RpcError(-32001, "File not found", {"reason": "file_not_found"})
        if method == "chat/history":
            _string(params, "project")
            return {"entries": [], "more": False}
        if method == "allowance/status":
            return {"codex": None, "claude": None}
        if method == "ids/attach":
            _string(params, "project")
            _string(params, "text")
            raise RpcError(-32601, "Method not found")
        if method == "settings/get":
            return dict(self.settings)
        if method == "settings/set":
            self.settings.update(params)
            return dict(self.settings)
        if method == "job/status":
            job = _string(params, "job")
            return self.jobs.get(job, {"job": job, "state": "unknown"})
        if method in {"job/plan", "job/sample", "job/snapshot", "job/outputs"}:
            job = _string(params, "job")
            return self.jobs.get(job, {"job": job, "state": "unknown"})
        if method == "auth/status":
            return {"claude": self.auth["claude"], "codex": self.auth["codex"]}
        if method in {"auth/claudeLogin", "auth/codexLogin"}:
            vendor = "claude" if method.startswith("auth/claude") else "codex"
            self.auth[vendor] = True
            return {"started": True, "vendor": vendor}
        if method == "setup/checklist":
            return []
        if method == "setup/runChecks":
            return {"items": []}
        if method == "setup/markManual":
            return {"item": _string(params, "item"), "status": "manual_done"}
        if method.startswith("job/"):
            job = _string(params, "job")
            state: dict[str, object] = {
                "job": job,
                "state": "running" if method == "job/startFull" else "accepted",
            }
            self.jobs[job] = state
            return state
        if method in {"chat/send", "chat/stop", "os/suspendHint", "os/resumeHint", "os/powerStatus"}:
            return {"status": "accepted"}
        raise RpcError(-32601, "Method not found")


class ApiService:
    METHODS = frozenset(
        {
            "app/status",
            "health/ping",
            "shutdown",
            "setup/checklist",
            "setup/runChecks",
            "setup/markManual",
            "settings/get",
            "settings/set",
            "projects/list",
            "projects/jobs",
            "projects/create",
            "files/list",
            "files/path",
            "chat/history",
            "allowance/status",
            "ids/attach",
            "chat/send",
            "chat/stop",
            "job/status",
            "job/approvePlan",
            "job/approveSample",
            "job/startFull",
            "job/pause",
            "job/resume",
            "job/rebuildRules",
            "job/plan",
            "job/sample",
            "job/snapshot",
            "job/outputs",
            "auth/claudeLogin",
            "auth/codexLogin",
            "auth/status",
            "os/suspendHint",
            "os/resumeHint",
            "os/powerStatus",
        }
    )

    def __init__(self, backend: ApiBackend | None = None, event_bus: EventBus | None = None) -> None:
        self.backend = backend or MemoryBackend()
        self.event_bus = event_bus
        self.notify: Notify | None = None
        self.stopping = False
        self._chat_tasks: dict[str, asyncio.Task[object]] = {}
        setter = getattr(self.backend, "set_chat_notify", None)
        if callable(setter):
            setter(self._notify_chat_event)

    async def _notify_chat_event(self, project: str, event: dict[str, object]) -> None:
        if self.notify is not None:
            await self.notify(
                {"jsonrpc": "2.0", "method": "chat/event", "params": {"project": project, "event": event}}
            )

    async def dispatch(self, method: str, params: object) -> object:
        if method not in self.METHODS:
            raise RpcError(-32601, "Method not found", {"method": method})
        if self.stopping and method != "shutdown":
            raise RpcError(-32001, "Engine is stopping", {"reason": "stopping"})
        data = _object(params)
        self._validate(method, data)
        if method == "app/status" and isinstance(self.backend, MemoryBackend):
            return {"state": "stopping" if self.stopping else "ready", "protocol": "2.0"}
        if method == "health/ping":
            return {"status": "ok"}
        if method == "shutdown" and isinstance(self.backend, MemoryBackend):
            self.stopping = True
            return {"stopping": True}
        if method == "chat/send":
            return await self._chat(data)
        result = await self.backend.call(method, data)
        if method == "shutdown":
            self.stopping = True
        return result

    def _validate(self, method: str, data: dict[str, object]) -> None:
        common: dict[str, set[str]] = {
            "setup/markManual": {"item"},
            "files/list": {"project"},
            "files/path": {"project", "name"},
            "projects/jobs": {"project"},
            "chat/history": {"project", "before", "limit"},
            "allowance/status": set(),
            "ids/attach": {"project", "text"},
            "projects/create": {"project", "title"},
            "chat/send": {"project", "text"},
            "chat/stop": {"project"},
            "job/status": {"job"},
            "job/plan": {"job", "specVersion"},
            "job/sample": {"job"},
            "job/snapshot": {"job"},
            "job/outputs": {"job"},
            "job/approvePlan": {"job", "expectedSpecVersion"},
            "job/approveSample": {"job", "expectedSpecVersion"},
            "job/startFull": {"job"},
            "job/pause": {"job"},
            "job/resume": {"job"},
            "job/rebuildRules": {"job", "rules"},
            "os/suspendHint": set(),
            "os/resumeHint": set(),
            "os/powerStatus": {"onAc", "percent"},
            "auth/claudeLogin": set(),
            "auth/codexLogin": set(),
            "settings/get": set(),
            "setup/checklist": set(),
            "setup/runChecks": set(),
            "projects/list": set(),
            "auth/status": set(),
            "app/status": set(),
            "health/ping": set(),
            "shutdown": set(),
        }
        if method == "settings/set":
            settings_allowed = {
                "connectorPath",
                "keyPath",
                "workerCount",
                "retentionDays",
                "claudeModel",
                "codexModel",
                "confirmModelChange",
            }
            _exact(data, settings_allowed)
            for key in ("connectorPath", "claudeModel", "codexModel"):
                if key in data and not isinstance(data[key], str):
                    raise RpcError(-32602, "Invalid params", {"field": key, "reason": "must be a string"})
            for key in ("workerCount", "retentionDays"):
                value = data.get(key)
                if key == "workerCount" and value is None:
                    continue
                if key in data and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
                    raise RpcError(
                        -32602, "Invalid params", {"field": key, "reason": "must be a positive integer"}
                    )
            if (
                any(key in data for key in ("claudeModel", "codexModel"))
                and data.get("confirmModelChange") is not True
            ):
                raise RpcError(
                    -32602,
                    "Invalid params",
                    {
                        "field": "confirmModelChange",
                        "reason": "confirmModelChange required to change model ids",
                    },
                )
            return
        allowed: set[str] | None = common.get(method)
        if allowed is not None:
            _exact(data, allowed)
        if method == "chat/history":
            if "before" in data and (isinstance(data["before"], bool) or not isinstance(data["before"], int)):
                raise RpcError(-32602, "Invalid params", {"field": "before", "reason": "must be an integer"})
            if "limit" in data and (
                isinstance(data["limit"], bool)
                or not isinstance(data["limit"], int)
                or not 1 <= data["limit"] <= 500
            ):
                raise RpcError(
                    -32602, "Invalid params", {"field": "limit", "reason": "must be between 1 and 500"}
                )
        if method == "os/powerStatus":
            on_ac, percent = data.get("onAc"), data.get("percent")
            if on_ac is not None and not isinstance(on_ac, bool):
                raise RpcError(
                    -32602, "Invalid params", {"field": "onAc", "reason": "must be boolean or null"}
                )
            if percent is not None and (isinstance(percent, bool) or not isinstance(percent, int)):
                raise RpcError(
                    -32602, "Invalid params", {"field": "percent", "reason": "must be integer or null"}
                )
            if isinstance(percent, int) and not 0 <= percent <= 100:
                raise RpcError(
                    -32602,
                    "Invalid params",
                    {"field": "percent", "reason": "must be between 0 and 100"},
                )
        if method == "job/plan" and "specVersion" in data:
            _int(data, "specVersion")
        required: dict[str, tuple[str, ...]] = {
            "setup/markManual": ("item",),
            "files/list": ("project",),
            "projects/create": ("project", "title"),
            "chat/send": ("project", "text"),
            "chat/stop": ("project",),
            "job/status": ("job",),
            "job/plan": ("job",),
            "job/sample": ("job",),
            "job/snapshot": ("job",),
            "job/outputs": ("job",),
            "job/approvePlan": ("job", "expectedSpecVersion"),
            "job/approveSample": ("job", "expectedSpecVersion"),
            "job/startFull": ("job",),
            "job/pause": ("job",),
            "job/resume": ("job",),
            "job/rebuildRules": ("job", "rules"),
        }
        for key in required.get(method, ()):
            if key == "expectedSpecVersion":
                _int(data, key)
            elif key == "rules":
                if not isinstance(data[key], list):
                    raise RpcError(-32602, "Invalid params", {"field": key, "reason": "must be an array"})
            else:
                _string(data, key)

    async def _chat(self, data: dict[str, object]) -> object:
        project, text_value = _string(data, "project"), _string(data, "text")
        result = await self.backend.call("chat/send", {"project": project, "text": text_value})
        if self.notify is not None:
            await self.notify(
                {"jsonrpc": "2.0", "method": "chat/event", "params": {"project": project, "event": result}}
            )
        return result

    async def publish_event(self, event: Event) -> None:
        if self.notify is not None:
            params = {"kind": event.kind, "job": event.job_id, **event.data}
            await self.notify({"jsonrpc": "2.0", "method": "engine/event", "params": params})
            if event.job_id is not None and event.kind == "counts":

                def count_field(name: str) -> int:
                    value = event.data.get(name, 0)
                    return value if isinstance(value, int) and not isinstance(value, bool) else 0

                done = count_field("done")
                remaining = count_field("remaining")
                in_flight = count_field("in_flight")
                failed = count_field("failed")
                held_back = count_field("held_back")
                progress = {
                    "job": event.job_id,
                    "project": event.data.get("project"),
                    "phase": event.data.get("phase", "full"),
                    "done": done,
                    "inFlight": in_flight,
                    "retrying": count_field("retrying"),
                    "failed": failed,
                    "heldBack": held_back,
                    "remaining": remaining,
                    "total": count_field("total"),
                }
                progress = {key: value for key, value in progress.items() if value is not None}
                await self.notify({"jsonrpc": "2.0", "method": "job/progress", "params": progress})
            if event.job_id is not None and event.kind == "state" and event.data.get("source") == "store":
                step = str(event.data.get("to", event.data.get("state", "")))
                state_params = {
                    "job": event.job_id,
                    "project": event.data.get("project"),
                    "state": public_state(step),
                    "step": step,
                }
                state_params = {key: value for key, value in state_params.items() if value is not None}
                await self.notify({"jsonrpc": "2.0", "method": "job/state", "params": state_params})


class JsonRpcServer:
    def __init__(
        self, service: ApiService, request_timeout: float = 30.0, logger: logging.Logger | None = None
    ) -> None:
        if request_timeout <= 0:
            raise ValueError("request_timeout must be positive")
        self.service, self.request_timeout = service, request_timeout
        self.logger = logger or logging.getLogger("bca.api")
        self._write_lock = asyncio.Lock()

    async def response(self, message: object) -> Json | None:
        if not isinstance(message, dict):
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
        data = cast(dict[str, object], message)
        request_id = data.get("id")
        valid_id = isinstance(request_id, (str, int)) and not isinstance(request_id, bool)
        if (
            data.get("jsonrpc") != "2.0"
            or not isinstance(data.get("method"), str)
            or ("id" in data and not valid_id)
        ):
            return {
                "jsonrpc": "2.0",
                "id": request_id if valid_id else None,
                "error": {"code": -32600, "message": "Invalid Request"},
            }
        try:
            dispatch = self.service.dispatch(cast(str, data["method"]), data.get("params", {}))
            result = (
                await dispatch
                if data["method"] == "chat/send"
                else await asyncio.wait_for(dispatch, self.request_timeout)
            )
            if "id" not in data:
                return None
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except TimeoutError:
            if "id" not in data:
                return None
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32000, "message": "Request timed out"},
            }
        except RpcError as exc:
            if "id" not in data:
                return None
            error: Json = {"code": exc.code, "message": exc.message}
            if exc.data is not None:
                error["data"] = exc.data
            return {"jsonrpc": "2.0", "id": request_id, "error": error}
        except Exception:
            self.logger.exception("API request failed")
            if "id" not in data:
                return None
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32603, "message": "Internal error"},
            }

    async def serve(self, reader: TextIO, writer: TextIO) -> None:
        async def send(message: Json) -> None:
            async with self._write_lock:
                writer.write(
                    json.dumps(
                        strict_json_value(message, self.logger),
                        separators=(",", ":"),
                        ensure_ascii=False,
                        allow_nan=False,
                    )
                    + "\n"
                )
                writer.flush()

        self.service.notify = send
        subscription = self.service.event_bus.subscribe() if self.service.event_bus is not None else None
        loop = asyncio.get_running_loop()

        async def forward_events() -> None:
            if subscription is None:
                return
            wake = asyncio.Event()

            def signal_wake() -> None:
                loop.call_soon_threadsafe(wake.set)

            subscription.set_wake(signal_wake)
            while not self.service.stopping:
                for event in subscription.drain():
                    await self.service.publish_event(event)
                await wake.wait()
                wake.clear()

        event_task = asyncio.create_task(forward_events())
        request_tasks: set[asyncio.Task[None]] = set()

        async def handle(message: object) -> None:
            result = await self.response(message)
            if result is not None:
                await send(result)

        def finished(task: asyncio.Task[None]) -> None:
            request_tasks.discard(task)
            if not task.cancelled() and task.exception() is not None:
                self.logger.error("API request task failed", exc_info=task.exception())
            if self.service.stopping:
                # The reader thread may remain blocked on an open stdin.  Wake
                # the event-loop reader after the shutdown request is complete.
                lines.put_nowait(None)

        lines: asyncio.Queue[str | None] = asyncio.Queue()
        stop_reader = threading.Event()

        def read_lines() -> None:
            while not stop_reader.is_set():
                line = reader.readline()
                try:
                    loop.call_soon_threadsafe(lines.put_nowait, line or None)
                except RuntimeError:
                    return
                if not line:
                    return

        threading.Thread(target=read_lines, name="bca-api-stdin", daemon=True).start()

        try:
            while not self.service.stopping:
                line = await lines.get()
                if self.service.stopping:
                    break
                if line is None:
                    await self.service.dispatch("shutdown", {})
                    break
                try:
                    message: object = json.loads(line)
                except json.JSONDecodeError as exc:
                    await send(
                        {
                            "jsonrpc": "2.0",
                            "id": None,
                            "error": {
                                "code": -32700,
                                "message": "Parse error",
                                "data": {"detail": str(exc)},
                            },
                        }
                    )
                    continue
                if isinstance(message, dict) and message.get("method") == "shutdown":
                    # Shutdown must get control even when an earlier request is
                    # waiting on a long-running chat turn.  The backend stops
                    # those turns before it stops job drivers; the other
                    # request tasks are cancelled during teardown below.
                    await asyncio.sleep(0)
                    await handle(message)
                    break
                task = asyncio.create_task(handle(message))
                request_tasks.add(task)
                task.add_done_callback(finished)
        finally:
            stop_reader.set()
            for task in tuple(request_tasks):
                task.cancel()
            if request_tasks:
                await asyncio.gather(*request_tasks, return_exceptions=True)
            event_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await event_task
            if subscription is not None and self.service.event_bus is not None:
                self.service.event_bus.unsubscribe(subscription)


async def run_stdio(backend: ApiBackend | None = None) -> None:
    app_data = getattr(backend, "app_data", None)
    log_path = Path(app_data) / "logs" / "bca-api.log" if app_data is not None else None
    try:
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            logging.basicConfig(filename=log_path, level=logging.INFO)
        else:
            logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    except OSError:
        logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    request_timeout = 30.0
    configured_timeout = os.environ.get("BCA_API_REQUEST_TIMEOUT")
    if configured_timeout is not None:
        try:
            request_timeout = float(configured_timeout)
        except ValueError as exc:
            raise ValueError("BCA_API_REQUEST_TIMEOUT must be a positive number") from exc
    event_bus = getattr(backend, "event_bus", None)
    server = JsonRpcServer(ApiService(backend, event_bus=event_bus), request_timeout=request_timeout)
    protocol_fd = os.dup(sys.stdout.fileno())
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    writer = os.fdopen(protocol_fd, "w", encoding="utf-8", buffering=1, newline="\n")
    try:
        await server.serve(sys.stdin, writer)
    finally:
        writer.close()
