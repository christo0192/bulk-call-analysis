from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Protocol, cast

from jsonschema import ValidationError
from jsonschema.validators import validator_for

from .appserver import AppServer
from .quota import (
    ALLOW,
    INTERRUPT_ALL,
    STOP_DISPATCH,
    Decision,
    QuotaGuard,
    QuotaSnapshot,
    merge_update,
    parse_rate_limits,
)
from .rpc import RpcError, RpcTimeout


@dataclass(frozen=True)
class LeadTask:
    lead_id: str
    attempt_id: str
    prompt: str
    output_schema: dict[str, object]
    model: str
    effort: str
    repair: bool = False
    spec_version: int = 0


class PoolCallbacks(Protocol):
    async def next_tasks(self, n: int) -> list[LeadTask]: ...

    async def on_started(self, task: LeadTask) -> bool | None: ...

    async def on_result(
        self, task: LeadTask, answer: dict[str, Any], model_used: str, usage: dict[str, Any] | None
    ) -> None: ...

    async def on_failure(self, task: LeadTask, error_code: str, counted: bool, detail: str) -> None: ...

    async def on_activity(self, ts: float) -> None: ...

    async def on_quota(self, snapshot: QuotaSnapshot, decision: Decision) -> None: ...

    async def on_reroute(self, task: LeadTask, from_model: str, to_model: str, reason: str) -> None: ...

    async def on_pause(self, reason: str, resume_at: int | None) -> None: ...

    def set_isolation_leads(self, lead_ids: frozenset[str]) -> frozenset[str]: ...


class PoolClock(Protocol):
    def now(self) -> float: ...


@dataclass
class _Lead:
    task: LeadTask
    thread_id: str | None = None
    turn_id: str | None = None
    model_used: str = ""
    text: str = ""
    usage: dict[str, Any] | None = None
    done: bool = False
    outcome_delivered: bool = False
    pending_action: asyncio.Task[None] | None = None
    server_generation: int = 0
    completion_event: asyncio.Event | None = None


class LunaPool:
    def __init__(
        self,
        launch_factory: Callable[[], Awaitable[AppServer]],
        callbacks: PoolCallbacks,
        guard: QuotaGuard,
        concurrency: int,
        lead_timeout_s: float,
        interrupt_grace_s: float = 60,
        recycle_every: int = 500,
        max_consecutive_failures: int = 25,
        crash_window_s: float = 600,
        restart_backoff_s: float = 0,
        retry_backoff_s: float = 1,
        rpc_timeout_s: float = 30,
        clock: PoolClock | None = None,
        can_dispatch: Callable[[], bool] | None = None,
    ) -> None:
        self.launch_factory = launch_factory
        self.callbacks = callbacks
        self.guard = guard
        self.concurrency = max(1, concurrency)
        self.lead_timeout_s = lead_timeout_s
        self.interrupt_grace_s = interrupt_grace_s
        self.recycle_every = max(1, recycle_every)
        self.max_consecutive_failures = max(1, max_consecutive_failures)
        # Retained for configuration compatibility; crash storms are bounded by
        # the pool lifetime and reset when a paused pool resumes.
        self.crash_window_s = max(0, crash_window_s)
        self.restart_backoff_s = max(0, restart_backoff_s)
        self.retry_backoff_s = max(0, retry_backoff_s)
        self.rpc_timeout_s = max(0, rpc_timeout_s)
        self.clock = clock
        self.can_dispatch = can_dispatch
        self._server: AppServer | None = None
        self._leads: dict[str, _Lead] = {}
        self._by_turn: dict[str, _Lead] = {}
        self._snapshot: QuotaSnapshot | None = None
        self._paused = False
        self._stopping = False
        self._consecutive_failures = 0
        self._completed = 0
        self._backoff = 0.0
        self._restart_lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._crashes: list[float] = []
        self._models: set[str] = set()
        self._needs_recycle = False
        self._pending_actions: set[asyncio.Task[None]] = set()
        self._rate_result: dict[str, Any] | None = None
        self._latched_decision: Decision | None = None
        self._dispatch_closed = False
        self._server_generation = 0
        self._pending_pause: tuple[str, int | None] | None = None
        self._next_tasks: asyncio.Task[list[LeadTask]] | None = None
        self._interrupt_task: asyncio.Task[None] | None = None
        self._isolation_leads: set[str] = set()
        self._restarted_crash_generations: set[int] = set()

    def set_concurrency(self, n: int) -> None:
        self.concurrency = max(1, n)

    @property
    def claimed_lead_ids(self) -> frozenset[str]:
        """Lead claims currently owned by dispatched pool workers."""
        return frozenset(lead.task.lead_id for lead in self._leads.values())

    @property
    def pending_pause(self) -> tuple[str, int | None] | None:
        """Pause that is waiting for active turns to drain, if any."""
        return self._pending_pause

    async def pause(self, reason: str) -> None:
        """Stop dispatching and, for interrupting reasons, stop active turns."""
        if self._paused:
            if reason != "model_unavailable" and self._leads:
                self._schedule_interrupt(reason)
            return
        self._paused = True
        self._dispatch_closed = True
        next_tasks = self._next_tasks
        if next_tasks is not None and next_tasks is not asyncio.current_task() and not next_tasks.done():
            next_tasks.cancel()
        if reason != "model_unavailable":
            self._schedule_interrupt(reason)

    def _schedule_interrupt(self, reason: str) -> None:
        task = self._interrupt_task
        if task is None or task.done():
            self._interrupt_task = asyncio.create_task(self._interrupt_all(reason))

    async def run_until_idle(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._restart_lock = asyncio.Lock()
            self._loop = loop
        was_paused = self._paused
        self._stopping = False
        self._paused = False
        if was_paused:
            self._crashes.clear()
            self._restarted_crash_generations.clear()
        self._latched_decision = None
        self._dispatch_closed = False
        self._consecutive_failures = 0
        self._backoff = 0
        self._needs_recycle = False
        await self._launch_and_prepare()
        running: set[asyncio.Task[None]] = set()
        heartbeat = asyncio.create_task(self._heartbeat())
        try:
            while not self._stopping and not self._paused:
                await self._wait_for_restart()
                error = self._notification_error()
                if error is not None:
                    raise RuntimeError("app-server notification callback failed") from error
                await self._check_quota()
                if self._paused:
                    break
                effective_concurrency = 1 if self._isolation_leads else self.concurrency
                room = effective_concurrency - len(running)
                tasks: list[LeadTask] = []
                if room > 0:
                    if self._dispatch_closed:
                        break
                    next_tasks = asyncio.create_task(self.callbacks.next_tasks(room))
                    self._next_tasks = next_tasks
                    try:
                        tasks = await next_tasks
                    except asyncio.CancelledError:
                        if not self._paused:
                            raise
                        tasks = []
                    finally:
                        if self._next_tasks is next_tasks:
                            self._next_tasks = None
                    await self._wait_for_restart()
                    effective_concurrency = 1 if self._isolation_leads else self.concurrency
                    room = max(0, effective_concurrency - len(running))
                    if self._paused or self._server is None:
                        await asyncio.gather(*(self._fail_unstarted(item) for item in tasks))
                        if self._paused:
                            break
                        continue
                    dispatch_tasks, unstarted = tasks[:room], tasks[room:]
                    if unstarted:
                        await asyncio.gather(*(self._fail_unstarted(item) for item in unstarted))
                    for index, task in enumerate(dispatch_tasks):
                        if not await self._before_dispatch(task.model):
                            remaining = dispatch_tasks[index:]
                            await asyncio.gather(*(self._fail_unstarted(item) for item in remaining))
                            break
                        if task.model not in self._models:
                            remaining = dispatch_tasks[index:]
                            await asyncio.gather(*(self._fail_unstarted(item) for item in remaining))
                            await self._pause("model_unavailable", None)
                            break
                        lead = _Lead(task)
                        self._leads[task.attempt_id] = lead
                        running.add(asyncio.create_task(self._run_lead(lead)))
                if not running and not tasks and (self._paused or self._stopping):
                    break
                if not running and not tasks:
                    break
                if running:
                    done, running = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
                    for completed in done:
                        await completed
                await self._wait_for_restart()
                error = self._notification_error()
                if error is not None:
                    raise RuntimeError("app-server notification callback failed") from error
                if self._needs_recycle and not running and not self._paused:
                    self._needs_recycle = False
                    await self._restart("appserver_recycle")
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            if running:
                if self._paused and not self._stopping:
                    await asyncio.gather(*running, return_exceptions=True)
                else:
                    for running_task in running:
                        running_task.cancel()
                    await asyncio.gather(*running, return_exceptions=True)
            try:
                if self._pending_pause is not None:
                    reason, resume_at = self._pending_pause
                    self._pending_pause = None
                    await self.callbacks.on_pause(reason, resume_at)
            finally:
                try:
                    if self._pending_actions:
                        await asyncio.gather(*self._pending_actions, return_exceptions=True)
                finally:
                    if self._server is not None:
                        try:
                            await self._server.stop()
                        finally:
                            self._server = None

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(5)
            await self.callbacks.on_activity(self.clock.now() if self.clock is not None else time.time())

    async def stop(self) -> None:
        self._stopping = True
        self._dispatch_closed = True
        for lead in list(self._leads.values()):
            await self._fail(lead, "stopped", False, "pool stopped")
        server = self._server
        if server is not None:
            await asyncio.gather(*(self._interrupt(lead) for lead in list(self._leads.values())))
            await server.stop()
            self._server = None

    async def interrupt_all(self, reason: str) -> None:
        """Interrupt active turns and deliver one uncounted outcome for each."""
        await self._interrupt_all(reason)

    async def _launch_and_prepare(self) -> None:
        self._server = await self.launch_factory()
        self._server_generation += 1
        server = self._server
        if server is None:
            raise RuntimeError("app-server launch returned no server")
        client = server.client
        client.on_any_notification(self._notification)
        rate = await self._request_unlocked("account/rateLimits/read")
        self._rate_result = rate
        models = await self._request_unlocked("model/list", {"includeHidden": True})
        self._models = self._model_names(models)
        cursor = models.get("nextCursor")
        while isinstance(cursor, str) and cursor:
            models = await self._request_unlocked("model/list", {"includeHidden": True, "cursor": cursor})
            self._models.update(self._model_names(models))
            cursor = models.get("nextCursor")
        model = self._model_for_setup(rate)
        self._snapshot = parse_rate_limits(rate, model)
        await self._report_quota()

    async def _wait_for_restart(self) -> None:
        async with self._restart_lock:
            return

    def _notification_error(self) -> Exception | None:
        server = self._server
        return server.client.callback_error if server is not None else None

    async def _request_unlocked(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        server = self._server
        if server is None:
            raise RuntimeError("Luna pool restart did not produce an app-server")
        return await server.client.request(method, params, timeout_s=self.rpc_timeout_s)

    async def _request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        await self._wait_for_restart()
        return await self._request_unlocked(method, params)

    @staticmethod
    def _model_names(result: dict[str, Any]) -> set[str]:
        models = result.get("data", [])
        if not isinstance(models, list):
            return set()
        names: set[str] = set()
        for item in models:
            if isinstance(item, str):
                names.add(item)
            elif isinstance(item, dict):
                for key in ("slug", "id", "model"):
                    if isinstance(item.get(key), str):
                        names.add(item[key])
        return names

    @staticmethod
    def _model_for_setup(result: dict[str, Any]) -> str:
        limits = result.get("rateLimits")
        if isinstance(limits, dict) and isinstance(limits.get("normalModelSlug"), str):
            return str(limits["normalModelSlug"])
        by_limit = result.get("rateLimitsByLimitId")
        if isinstance(by_limit, dict):
            for value in by_limit.values():
                if isinstance(value, dict) and isinstance(value.get("normalModelSlug"), str):
                    return str(value["normalModelSlug"])
        return "codex"

    @staticmethod
    def _model_available(result: dict[str, Any], model: str) -> bool:
        models = result.get("data", [])
        if not isinstance(models, list):
            return False
        for item in models:
            if isinstance(item, str) and item == model:
                return True
            if isinstance(item, dict) and model in {item.get("slug"), item.get("id"), item.get("model")}:
                return True
        return False

    async def _report_quota(self) -> Decision:
        if self._snapshot is None:
            return Decision(ALLOW, "quota is unavailable", None)
        decision = self._latched_decision or self.guard.decide(self._snapshot)
        await self.callbacks.on_quota(self._snapshot, decision)
        return decision

    async def _check_quota(self) -> None:
        decision = await self._report_quota()
        if decision.kind == STOP_DISPATCH:
            await self._pause("quota", decision.resume_at)
        elif decision.kind == INTERRUPT_ALL:
            self._latched_decision = decision
            await self._interrupt_all(
                "credit"
                if "credit" in decision.reason
                else "spend_control"
                if "subscription" in decision.reason
                else "quota"
            )

    async def _before_dispatch(self, model: str) -> bool:
        if self.can_dispatch is not None and not self.can_dispatch():
            await self._pause("power", None)
            return False
        if self._rate_result is not None:
            self._snapshot = parse_rate_limits(self._rate_result, model)
        decision = await self._report_quota()
        if self._dispatch_closed or self._latched_decision is not None:
            return False
        if decision.kind == ALLOW:
            if self._backoff:
                await asyncio.sleep(self._backoff)
            return not self._paused and not self._stopping
        if decision.kind == STOP_DISPATCH:
            await self._pause("quota", decision.resume_at)
        else:
            self._latched_decision = decision
            await self._interrupt_all(
                "credit"
                if "credit" in decision.reason
                else "spend_control"
                if "subscription" in decision.reason
                else "quota"
            )
        return False

    async def _run_lead(self, lead: _Lead) -> None:
        task = lead.task
        callback_started = False
        try:
            callback_started_result = await self.callbacks.on_started(task)
            if callback_started_result is False:
                return
            callback_started = True
            await self._wait_for_restart()
            if self._paused or self._server is None:
                await self._fail(lead, "appserver_restart", False, "app-server unavailable before start")
                return
            lead.server_generation = self._server_generation
            if self._server is not None and self._server.returncode is not None:
                await self._crash_or_failure(lead, f"app-server exited with code {self._server.returncode}")
                return
            thread_start_result = await self._request(
                "thread/start",
                {
                    "model": task.model,
                    "ephemeral": True,
                    "approvalPolicy": "never",
                    "sandbox": "read-only",
                    "serviceTier": "default",
                },
            )
            thread = thread_start_result.get("thread")
            lead.thread_id = str(thread.get("id")) if isinstance(thread, dict) else None
            if lead.thread_id is None:
                raise ValueError("thread/start returned no thread id")
            if self._dispatch_closed or lead.outcome_delivered:
                return
            result = await asyncio.wait_for(
                self._request(
                    "turn/start",
                    {
                        "threadId": lead.thread_id,
                        "input": [{"type": "text", "text": task.prompt}],
                        "outputSchema": task.output_schema,
                        "model": task.model,
                        "effort": task.effort,
                        "summary": "concise",
                        "approvalPolicy": "never",
                        "sandboxPolicy": {"type": "readOnly"},
                        "serviceTierForTurn": "default",
                    },
                ),
                self.lead_timeout_s,
            )
            turn = result.get("turn")
            if isinstance(turn, dict) and turn.get("id"):
                lead.turn_id = str(turn["id"])
                self._by_turn[lead.turn_id] = lead
            if lead.outcome_delivered:
                await self._interrupt(lead)
                return
            await self._complete_from_events(lead)
        except RpcTimeout as exc:
            await self._crash_or_failure(lead, str(exc))
        except TimeoutError:
            await self._fail_timeout(lead)
        except (ConnectionError, BrokenPipeError) as exc:
            await self._crash_or_failure(lead, str(exc))
        except RpcError as exc:
            await self._handle_rpc_error(lead, exc)
        except (json.JSONDecodeError, ValidationError) as exc:
            await self._fail(lead, "invalid_output", True, str(exc))
        except Exception as exc:
            await self._fail(lead, "other", True, str(exc))
        finally:
            if lead.pending_action is not None:
                await asyncio.gather(lead.pending_action, return_exceptions=True)
            if callback_started and not lead.outcome_delivered:
                await self._fail(lead, "appserver_restart", False, "lead ended without an outcome")
            if (
                lead.thread_id is not None
                and lead.server_generation == self._server_generation
                and not self._restart_lock.locked()
            ):
                with contextlib.suppress(
                    ConnectionError, BrokenPipeError, RpcError, RpcTimeout, RuntimeError
                ):
                    await self._request("thread/unsubscribe", {"threadId": lead.thread_id})
            self._leads.pop(task.attempt_id, None)
            if lead.turn_id:
                self._by_turn.pop(lead.turn_id, None)
            self._refresh_isolation()

    async def _complete_from_events(self, lead: _Lead) -> None:
        lead.completion_event = asyncio.Event()
        if not lead.done:
            try:
                await asyncio.wait_for(lead.completion_event.wait(), self.lead_timeout_s)
            except TimeoutError:
                if not lead.done:
                    await self._fail_timeout(lead)
                return
        if lead.outcome_delivered:
            return
        if lead.text == "":
            await self._fail(lead, "invalid_output", True, "no agent message was returned")
            return
        answer = json.loads(lead.text)
        if not isinstance(answer, dict):
            raise ValidationError("output must be an object")
        validator_for(lead.task.output_schema).check_schema(lead.task.output_schema)
        validator = validator_for(lead.task.output_schema)(lead.task.output_schema)
        validator.validate(answer)
        lead.outcome_delivered = True
        try:
            await self.callbacks.on_result(lead.task, answer, lead.model_used or lead.task.model, lead.usage)
        except Exception:
            await self._callback_error(lead, "result callback failed")
            return
        self._completed += 1
        self._consecutive_failures = 0
        self._crashes.clear()
        if self._completed % self.recycle_every == 0:
            self._needs_recycle = True

    async def _notification(self, message: dict[str, Any]) -> None:
        await self.callbacks.on_activity(self.clock.now() if self.clock is not None else time.time())
        method = message.get("method", "")
        params = message.get("params")
        if not isinstance(params, dict):
            return
        thread_id = str(params.get("threadId")) if params.get("threadId") is not None else None
        turn_id = str(params.get("turnId")) if params.get("turnId") is not None else None
        lead = self._by_turn.get(turn_id) if turn_id else None
        if lead is None and thread_id:
            lead = next((item for item in self._leads.values() if item.thread_id == thread_id), None)
        if method == "account/rateLimits/updated":
            if self._snapshot is not None:
                self._snapshot = merge_update(self._snapshot, params)
                decision = self.guard.decide(self._snapshot)
                if decision.kind != ALLOW:
                    self._latched_decision = decision
                    self._dispatch_closed = True
                    self._schedule_action(self._check_quota())
            return
        if method == "model/rerouted" and lead is not None:
            from_model = str(params.get("fromModel", lead.model_used or lead.task.model))
            to_model = str(params.get("toModel", params.get("model", lead.task.model)))
            lead.model_used = to_model
            await self.callbacks.on_reroute(lead.task, from_model, to_model, str(params.get("reason", "")))
        if method == "thread/tokenUsage/updated" and lead is not None:
            usage = params.get("tokenUsage")
            if isinstance(usage, dict):
                lead.usage = usage
            return
        if method.endswith("requestApproval"):
            if lead is not None:
                await self._interrupt(lead)
                await self._fail(lead, "tool_call_forbidden", True, "approval request forbidden")
            return
        if method == "error":
            if params.get("willRetry") is True:
                return
            if lead is not None:
                await self._handle_error_notification(lead, params)
            return
        item = params.get("item")
        if lead is not None and isinstance(item, dict) and self._forbidden_item(item):
            await self._fail(lead, "tool_call_forbidden", True, str(item.get("type", "tool call")))
            lead.pending_action = asyncio.create_task(self._interrupt(lead))
        if lead is not None and method == "item/agentMessage/delta":
            lead.text += str(params.get("delta", ""))
        if (
            lead is not None
            and method == "item/completed"
            and isinstance(item, dict)
            and item.get("type") == "agentMessage"
            and isinstance(item.get("text"), str)
        ):
            lead.text = item["text"]
        if lead is not None and method == "turn/completed":
            turn = params.get("turn")
            if isinstance(turn, dict):
                status = turn.get("status")
                if status == "failed":
                    error = (
                        cast(dict[str, Any], turn.get("error")) if isinstance(turn.get("error"), dict) else {}
                    )
                    code = self._error_code(error.get("codexErrorInfo", error))
                    await self._handle_error_code(lead, code, str(error))
                elif status == "completed":
                    lead.done = True
                else:
                    lead.done = True
            if lead.completion_event is not None:
                lead.completion_event.set()

    @staticmethod
    def _forbidden_item(item: dict[str, Any]) -> bool:
        item_type = str(item.get("type", "")).lower()
        return item_type not in {"agentmessage", "reasoning", "usermessage"}

    async def _handle_error_notification(self, lead: _Lead, params: dict[str, Any]) -> None:
        error = params.get("error")
        if not isinstance(error, dict):
            error = params
        info = error.get("codexErrorInfo", error.get("code", "other"))
        code = self._error_code(info)
        await self._handle_error_code(lead, code, str(error.get("message", "")))

    async def _handle_rpc_error(self, lead: _Lead, error: RpcError) -> None:
        code = self._error_code(error.data) if error.data is not None else str(error.code)
        await self._handle_error_code(lead, code, error.message)

    @staticmethod
    def _error_code(value: Any) -> str:
        if isinstance(value, dict):
            for key in ("code", "type", "name", "codexErrorInfo"):
                if isinstance(value.get(key), str):
                    return str(value[key])
            if len(value) == 1:
                return str(next(iter(value)))
        return str(value)

    async def _handle_error_code(self, lead: _Lead, code: str, detail: str) -> None:
        retryable = {
            "rateLimitExceeded",
            "serverOverloaded",
            "httpConnectionFailed",
            "responseStreamConnectionFailed",
            "responseStreamDisconnected",
            "responseTooManyFailedAttempts",
            "-32001",
        }
        if code in retryable:
            self._backoff = self.retry_backoff_s
            self.set_concurrency(self.concurrency - 1)
            await self._fail(lead, code, False, detail)
        elif code == "usageLimitExceeded":
            self._dispatch_closed = True
            self._latched_decision = Decision(INTERRUPT_ALL, "usage limit exceeded", None)
            await self._fail(lead, code, False, detail)
            lead.pending_action = self._schedule_action(
                self._refresh_quota_after_usage_limit(lead.task.model)
            )
        elif code == "unauthorized":
            await self._fail(lead, code, False, detail)
            await self._pause("auth", None)
        elif code == "modelNotFound":
            await self._fail(lead, code, False, detail)
            await self._pause("model_unavailable", None)
        elif code in {"contextWindowExceeded", "badRequest"}:
            await self._fail(lead, code, True, detail)
        else:
            await self._fail(lead, code, True, detail)

    async def _fail_timeout(self, lead: _Lead) -> None:
        if self._server is not None and self._server.returncode is not None:
            await self._crash_or_failure(lead, f"app-server exited with code {self._server.returncode}")
            return
        await self._interrupt(lead)
        try:
            await asyncio.wait_for(self._wait_done(lead), self.interrupt_grace_s)
        except TimeoutError:
            await self._fail(lead, "timeout", True, "lead timeout")
            await self._restart("appserver_restart")
        else:
            await self._fail(lead, "timeout", True, "lead timeout")

    async def _wait_done(self, lead: _Lead) -> None:
        while not lead.done:
            await asyncio.sleep(0.01)

    async def _interrupt(self, lead: _Lead) -> None:
        if lead.turn_id is not None:
            with contextlib.suppress(ConnectionError, BrokenPipeError, RpcError, RpcTimeout):
                await self._request("turn/interrupt", {"threadId": lead.thread_id, "turnId": lead.turn_id})

    async def _interrupt_all(self, reason: str) -> None:
        self._dispatch_closed = True
        leads = list(self._leads.values())
        interrupted: list[_Lead] = []
        for lead in leads:
            if not lead.outcome_delivered:
                lead.outcome_delivered = True
                lead.done = True
                interrupted.append(lead)
        await asyncio.gather(*(self._interrupt(lead) for lead in leads))
        await asyncio.gather(
            *(self._deliver_failure(lead, "quota_interrupt", False, reason) for lead in interrupted)
        )
        decision = self._latched_decision
        resume_at = decision.resume_at if decision else None
        if resume_at is None and decision and decision.reason == "a rate-limit window is exhausted":
            resume_at = self._latest_reset()
        await self._pause(reason, resume_at)

    async def _fail(self, lead: _Lead, code: str, counted: bool, detail: str) -> None:
        if lead.outcome_delivered:
            return
        lead.outcome_delivered = True
        lead.done = True
        try:
            await self.callbacks.on_failure(lead.task, code, counted, detail)
        except Exception:
            await self._callback_error(lead, "failure callback failed")
            return
        if counted:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.max_consecutive_failures:
                await self._pause("error_storm", None)

    async def _pause(self, reason: str, resume_at: int | None) -> None:
        if self._paused:
            if self._pending_pause is None and not self._leads:
                await self.callbacks.on_pause(reason, resume_at)
            return
        self._paused = True
        self._dispatch_closed = True
        self._pending_pause = (reason, resume_at)
        if not self._leads:
            pending = self._pending_pause
            self._pending_pause = None
            if pending is not None:
                await self.callbacks.on_pause(*pending)

    async def _deliver_failure(self, lead: _Lead, code: str, counted: bool, detail: str) -> None:
        try:
            await self.callbacks.on_failure(lead.task, code, counted, detail)
        except Exception:
            await self._callback_error(lead, "failure callback failed")

    async def _callback_error(self, lead: _Lead, detail: str) -> None:
        attempt = getattr(self.callbacks, "runner", None)
        if attempt is not None:
            callback_runner = attempt
            attempt_id = callback_runner._attempts.get(lead.task.attempt_id)
            if attempt_id is not None:
                callback_runner._close_attempt(lead.task.attempt_id)
                with contextlib.suppress(ValueError, KeyError):
                    callback_runner.deps.store.fail_attempt(
                        attempt_id,
                        "callback_error",
                        True,
                        callback_runner.deps.config.limits.lead_max_attempts,
                    )
                callback_runner._get_phase_event().set()
        await asyncio.sleep(0)

    async def _refresh_quota_after_usage_limit(self, model: str) -> None:
        try:
            rate = await self._request("account/rateLimits/read")
            self._rate_result = rate
            self._snapshot = parse_rate_limits(rate, model)
        except Exception:
            pass
        decision = (
            self.guard.decide(self._snapshot) if self._snapshot else Decision(INTERRUPT_ALL, "quota", None)
        )
        self._latched_decision = decision
        resume_at = decision.resume_at if decision.resume_at is not None else self._latest_reset()
        await self._pause("quota", resume_at)

    def _latest_reset(self) -> int | None:
        if self._snapshot is None:
            return None
        return max(
            (
                window.resets_at
                for window in (self._snapshot.primary, self._snapshot.secondary)
                if window is not None and window.resets_at is not None
            ),
            default=None,
        )

    async def _fail_unstarted(self, task: LeadTask) -> None:
        await self.callbacks.on_failure(task, "not_dispatched", False, "dispatch closed before start")

    def _schedule_action(self, action: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = asyncio.create_task(action)
        self._pending_actions.add(task)
        task.add_done_callback(self._pending_actions.discard)
        return task

    async def _crash_or_failure(self, lead: _Lead, detail: str) -> None:
        await self._restart(
            "appserver_crash",
            crash=True,
            server_generation=lead.server_generation,
            crash_detail=detail,
            crash_lead=lead,
        )

    async def _restart(
        self,
        reason: str,
        crash: bool = False,
        server_generation: int | None = None,
        crash_detail: str | None = None,
        crash_lead: _Lead | None = None,
    ) -> None:
        async with self._restart_lock:
            if server_generation is not None and server_generation != self._server_generation:
                server = self._server
                if crash_lead is None or crash_lead.outcome_delivered:
                    return
                if server is not None and server.returncode is None:
                    await self._fail(crash_lead, "appserver_restart", False, crash_detail or reason)
                    return
                server_generation = self._server_generation
            if crash and server_generation in self._restarted_crash_generations:
                if crash_lead is not None and not crash_lead.outcome_delivered:
                    await self._fail(crash_lead, "appserver_restart", False, crash_detail or reason)
                return
            if crash and server_generation is not None:
                self._restarted_crash_generations.add(server_generation)
            active = [lead for lead in self._leads.values() if not lead.outcome_delivered] if crash else []
            if crash:
                self._isolation_leads.update(lead.task.lead_id for lead in active)
                self._refresh_isolation()
            if crash:
                self._crashes.append(time.monotonic())
            if self._server is not None:
                await self._server.stop()
            self._server = None
            for active_lead in active or list(self._leads.values()):
                if crash and active_lead is crash_lead:
                    await self._fail(active_lead, "appserver_crash", False, crash_detail or reason)
                else:
                    await self._fail(active_lead, reason, False, "app-server restarted")
            if len(self._crashes) >= 4:
                await self._pause("error_storm", None)
                return
            if self.restart_backoff_s:
                await asyncio.sleep(self.restart_backoff_s)
            try:
                await self._launch_and_prepare()
            except Exception:
                server = self._server
                self._server = None
                if server is not None:
                    await server.stop()
                await self._pause("error_storm", None)

    def _refresh_isolation(self) -> None:
        setter = getattr(self.callbacks, "set_isolation_leads", None)
        if setter is None:
            return
        result = setter(frozenset(self._isolation_leads))
        if not isinstance(result, frozenset):
            raise TypeError("set_isolation_leads must return a frozenset")
        self._isolation_leads = set(result)
