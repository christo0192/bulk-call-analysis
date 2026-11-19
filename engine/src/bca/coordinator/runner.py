from __future__ import annotations

import asyncio
import contextlib
import gzip
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from bca.bq.leads import IdIntake, resolve_lead_source
from bca.bq.pull import build_bundle, pull_transcripts, run_preflight
from bca.bq.runner import BqRunner
from bca.config import SiteConfig
from bca.luna.pool import LeadTask, LunaPool, PoolCallbacks
from bca.luna.quota import Decision, QuotaGuard, QuotaSnapshot
from bca.mask.leakscan import scan_lead
from bca.mask.masker import MaskingContext, load_harvest, mask_lead_stream
from bca.mask.names import harvest_seen_twice
from bca.monitor.eta import EtaEstimator
from bca.monitor.events import Event, EventBus, EventKind
from bca.monitor.liveness import Liveness
from bca.monitor.progress import counts
from bca.monitor.sleep import SleepDetector
from bca.outputs.excel import build_workbook
from bca.outputs.quotes import QuoteCheck, check_answer, repair_prompt
from bca.outputs.verify import verify_job
from bca.store import (
    ClaimError,
    JobState,
    LeadStatus,
    PauseReason,
    Store,
    TransitionError,
    approve_plan,
    approve_sample,
    load_latest_spec,
    pause,
    start_full_run,
    transition,
)
from bca.store import resume as resume_job


class Clock(Protocol):
    def now(self) -> float: ...

    def monotonic(self) -> float: ...


class AsyncLaunch(Protocol):
    def __call__(self) -> Awaitable[Any]: ...


class CoordinatorPool(Protocol):
    callbacks: PoolCallbacks

    def set_concurrency(self, n: int) -> None: ...

    async def pause(self, reason: str) -> None: ...

    async def run_until_idle(self) -> None: ...

    @property
    def pending_pause(self) -> tuple[str, int | None] | None: ...


MaskerFactory = Callable[..., MaskingContext]
MAX_UNCOUNTED_FAILURES = 4
LOGGER = logging.getLogger(__name__)
__all__ = ["scan_lead"]


@dataclass(frozen=True)
class RunnerDeps:
    store: Store
    bq: BqRunner
    masking_context: MaskerFactory
    launch_pool: AsyncLaunch
    clock: Clock | None
    event_bus: EventBus
    config: SiteConfig
    job_dir: Path
    ner: Any | None = None
    concurrency: int = 4
    approved_bytes: int | None = None
    retry_backoff_s: float = 1


def _as_dict(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


class _Callbacks(PoolCallbacks):
    def __init__(self, runner: JobRunner, sample: bool = False, dispatch_limit: int | None = None) -> None:
        self.runner = runner
        self.sample = sample
        self._tasks: list[LeadTask] = []
        self._claimed: set[str] = set()
        self._uncounted: dict[str, int] = {}
        self._dispatch_limit = dispatch_limit
        self.attempt_ended = False

    def set_tasks(self, tasks: list[LeadTask]) -> None:
        self._tasks = sorted(tasks, key=lambda task: task.lead_id)

    def _queue_tasks(self, tasks: list[LeadTask]) -> None:
        version = self.runner._job_spec_version()
        self._tasks.extend(task for task in tasks if task.spec_version == version)
        self._tasks.sort(key=lambda task: task.lead_id)

    @property
    def claimed_lead_ids(self) -> frozenset[str]:
        return frozenset(self._claimed)

    def set_isolation_leads(self, lead_ids: frozenset[str]) -> frozenset[str]:
        """Keep queued or active suspects; a counted repeat is no longer runnable."""
        if not lead_ids:
            self._isolation_leads = set()
            return frozenset()
        placeholders = ",".join("?" for _ in lead_ids)
        rows = self.runner.deps.store.rows(
            "SELECT lead_id FROM leads WHERE job_id=? AND status IN ('queued', 'in_flight') AND lead_id IN ("
            + placeholders
            + ")",
            (self.runner.job_id, *sorted(lead_ids)),
        )
        self._isolation_leads = {str(row[0]) for row in rows}
        return frozenset(self._isolation_leads)

    async def next_tasks(self, n: int) -> list[LeadTask]:
        while True:
            current_version = self.runner._job_spec_version()
            self._tasks = [task for task in self._tasks if task.spec_version == current_version]
            if self.runner._state() is not self.runner._phase_state(self.sample):
                return []
            if self._dispatch_limit == 0:
                return []
            if not self._tasks:
                if self._isolation_leads:
                    placeholders = ",".join("?" for _ in self._isolation_leads)
                    rows = self.runner.deps.store.rows(
                        "SELECT lead_id FROM leads WHERE job_id=? AND status='queued' "
                        "AND (?=0 OR in_sample=1) AND lead_id IN ("
                        + placeholders
                        + ") ORDER BY lead_id LIMIT ?",
                        (
                            self.runner.job_id,
                            int(self.sample),
                            *sorted(self._isolation_leads),
                            n,
                        ),
                    )
                    lead_ids = [str(row[0]) for row in rows]
                else:
                    lead_ids = self.runner.deps.store.next_queued(self.runner.job_id, n, self.sample)
                self._tasks = await self.runner._task_list(self.sample, lead_ids)
                if self.runner._state() is not self.runner._phase_state(self.sample):
                    return []
            if self._tasks:
                queued_ids = [task.lead_id for task in self._tasks]
                queued = {
                    str(row[0])
                    for row in self.runner.deps.store.rows(
                        "SELECT lead_id FROM leads WHERE job_id=? AND status='queued' "
                        "AND lead_id IN ({})".format(",".join("?" for _ in queued_ids)),
                        (self.runner.job_id, *queued_ids),
                    )
                }
                self._tasks = [task for task in self._tasks if task.lead_id in queued]
            if self._tasks:
                self._tasks.sort(key=lambda task: task.lead_id)
                limit = n if self._dispatch_limit is None else min(n, self._dispatch_limit)
                candidates, self._tasks = self._tasks[:limit], self._tasks[limit:]
                claimed = self.runner.deps.store.claim_queued(
                    self.runner.job_id,
                    [task.lead_id for task in candidates],
                    self.sample,
                    self.runner._bundle_metadata,
                )
                tasks = [task for task in candidates if task.lead_id in claimed]
                self._claimed.update(claimed)
                if self._dispatch_limit is not None:
                    self._dispatch_limit -= len(tasks)
                if tasks:
                    return tasks
            if self.runner._phase_finished(self.sample):
                return []
            phase_event = self.runner._get_phase_event()
            phase_event.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(phase_event.wait(), 1.0)

    async def on_started(self, task: LeadTask) -> bool:
        self._claimed.discard(task.lead_id)
        if (
            self.runner._state() is not self.runner._phase_state(self.sample)
            or task.spec_version != self.runner._job_spec_version()
        ):
            self.runner.deps.store.release_claim(self.runner.job_id, task.lead_id)
            self.runner._closed_attempts.add(task.attempt_id)
            return False
        try:
            attempt = self.runner.deps.store.start_attempt(
                self.runner.job_id,
                task.lead_id,
                task.model,
                counted=True,
                claimed=True,
                spec_version=task.spec_version,
            )
        except ClaimError:
            # A pause or another outcome can release a lazy claim after the
            # pool has selected the task but before its worker starts.
            self.runner._closed_attempts.add(task.attempt_id)
            self.runner.deps.store.release_claim(self.runner.job_id, task.lead_id)
            return False
        self.runner._attempts[task.attempt_id] = attempt
        self.runner._publish("lead_started", {"lead_id": task.lead_id, "attempt_id": attempt})
        self.runner._liveness.lead_started(task.lead_id, self.runner._ts())
        return True

    async def on_result(
        self, task: LeadTask, answer: dict[str, Any], model_used: str, usage: dict[str, Any] | None
    ) -> None:
        try:
            await self._on_result(task, answer, model_used, usage)
        except Exception as exc:
            LOGGER.error("result callback failed for attempt %s", task.attempt_id, exc_info=exc)
            await self.runner._callback_error(task.attempt_id)

    async def _on_result(
        self, task: LeadTask, answer: dict[str, Any], model_used: str, usage: dict[str, Any] | None
    ) -> None:
        attempt = self.runner._attempts.get(task.attempt_id)
        if attempt is None:
            LOGGER.debug("ignoring result for closed attempt %s", task.attempt_id)
            return
        status = self.runner.deps.store.rows(
            "SELECT status FROM leads WHERE job_id=? AND lead_id=?",
            (self.runner.job_id, task.lead_id),
        )
        if not status or str(status[0][0]) != LeadStatus.in_flight:
            self.runner._close_attempt(task.attempt_id)
            return
        self.runner._refresh_spec()
        bundle_text = await asyncio.to_thread(self.runner._bundle_text, task.lead_id)
        failures = check_answer(
            answer,
            self.runner.spec.quote_fields,
            bundle_text,
            self.runner.deps.config.recipe.speaker_labels,
        )
        ok = all(item.ok for item in failures)
        flags = [f"quote:{item.field}:{item.reason}" for item in failures if not item.ok]
        is_repair = task.lead_id in self.runner._quote_failures
        stored_answer = answer
        stored_flags: list[str] = flags
        if is_repair and not ok:
            # A failed repair keeps the original answer so the quote gate's
            # evidence remains attached to the answer that was reviewed.
            stored_answer, original_failures = self.runner._quote_failures[task.lead_id]
            stored_flags = [f"quote:{item.field}:{item.reason}" for item in original_failures]
        if self.runner._is_closed_attempt(task.attempt_id):
            LOGGER.debug("ignoring result for closed attempt %s", task.attempt_id)
            return
        accepted = self.runner.deps.store.record_result(
            self.runner.job_id,
            task.lead_id,
            attempt,
            stored_answer,
            ok,
            stored_flags,
            task.spec_version,
            model_used,
            usage,
            clear_repair_pending=is_repair,
        )
        self.runner._close_attempt(task.attempt_id)
        self.attempt_ended = True
        self.runner._get_phase_event().set()
        if not accepted:
            try:
                self.runner.deps.store.fail_attempt(
                    attempt, "stale_spec", False, self.runner.deps.config.limits.lead_max_attempts
                )
            except ValueError:
                LOGGER.warning("ignoring stale-spec result for closed attempt %s", task.attempt_id)
            self.runner.deps.store.set_lead_status(self.runner.job_id, task.lead_id, LeadStatus.queued, None)
            self._queue_tasks([replace(task, attempt_id=str(uuid.uuid4()))])
            return
        if not ok and not is_repair:
            self.runner.deps.store.set_lead_status(
                self.runner.job_id, task.lead_id, LeadStatus.queued, "quote_repair_pending"
            )
            self.runner._quote_failures[task.lead_id] = (answer, [item for item in failures if not item.ok])
            repair_tasks = await self.runner._task_list(self.sample, [task.lead_id])
            self._queue_tasks(repair_tasks)
        elif is_repair:
            self.runner.deps.store.set_lead_status(self.runner.job_id, task.lead_id, LeadStatus.done)
            self.runner._drop_lead_state(task.lead_id)
        self.runner._publish("lead_done", {"lead_id": task.lead_id, "quotes_ok": ok, "repair": is_repair})
        self.runner._publish_progress()
        self.runner._liveness.lead_ended(task.lead_id, self.runner._ts())
        self.runner._eta.completed(self.runner._ts())

    async def on_failure(self, task: LeadTask, error_code: str, counted: bool, detail: str) -> None:
        try:
            await self._on_failure(task, error_code, counted, detail)
        except Exception as exc:
            LOGGER.error("failure callback failed for attempt %s", task.attempt_id, exc_info=exc)
            await self.runner._callback_error(task.attempt_id)

    async def _on_failure(self, task: LeadTask, error_code: str, counted: bool, detail: str) -> None:
        self._claimed.discard(task.lead_id)
        self.attempt_ended = True
        self.runner._get_phase_event().set()
        attempt = self.runner._attempts.get(task.attempt_id)
        if attempt is None:
            if self.runner._is_closed_attempt(task.attempt_id):
                LOGGER.debug("ignoring failure for closed attempt %s", task.attempt_id)
                return
            # claim_queued() runs before on_started().  A pool dispatch decision,
            # cancellation, or startup exception can therefore report a task
            # with no attempt row to close.
            if self.runner.deps.store.release_claim(self.runner.job_id, task.lead_id):
                self._queue_tasks([replace(task, attempt_id=str(uuid.uuid4()))])
        else:
            if self.runner._is_closed_attempt(task.attempt_id):
                LOGGER.debug("ignoring failure for closed attempt %s", task.attempt_id)
                return
            if error_code == "appserver_crash" and not counted:
                crash_count = self.runner.deps.store.count_appserver_crashes(self.runner.job_id, task.lead_id)
                if crash_count >= 2:
                    error_code = "appserver_crash_repeated"
                    counted = True
            self.runner._close_attempt(task.attempt_id)
            try:
                self.runner.deps.store.fail_attempt(
                    attempt,
                    error_code,
                    counted,
                    self.runner.deps.config.limits.lead_max_attempts,
                    clear_repair_pending=task.lead_id in self.runner._quote_failures and counted,
                )
            except ValueError:
                LOGGER.warning("ignoring failure for closed attempt %s", task.attempt_id)
                if self.runner.deps.store.release_claim(self.runner.job_id, task.lead_id):
                    self._queue_tasks([replace(task, attempt_id=str(uuid.uuid4()))])
            if task.lead_id in self.runner._quote_failures:
                if counted:
                    self.runner.deps.store.set_lead_status(
                        self.runner.job_id, task.lead_id, LeadStatus.done, "quote repair failed; kept answer"
                    )
                    self.runner._drop_lead_state(task.lead_id)
                else:
                    self.runner.deps.store.set_lead_status(
                        self.runner.job_id, task.lead_id, LeadStatus.queued, "quote_repair_pending"
                    )
                    retries = self.runner.deps.store.count_uncounted_failures(
                        self.runner.job_id, task.lead_id
                    )
                    self._uncounted[task.lead_id] = retries
                    if retries >= MAX_UNCOUNTED_FAILURES and error_code not in {
                        "appserver_crash",
                        "appserver_restart",
                    }:
                        await self.on_pause("network", None)
                    else:
                        self._queue_tasks([replace(task, attempt_id=str(uuid.uuid4()))])
            elif not counted:
                retries = self.runner.deps.store.count_uncounted_failures(self.runner.job_id, task.lead_id)
                self._uncounted[task.lead_id] = retries
                if retries >= MAX_UNCOUNTED_FAILURES and error_code not in {
                    "appserver_crash",
                    "appserver_restart",
                }:
                    await self.on_pause("network", None)
                else:
                    self._queue_tasks([replace(task, attempt_id=str(uuid.uuid4()))])
            else:
                status = self.runner.deps.store.rows(
                    "SELECT status FROM leads WHERE job_id=? AND lead_id=?",
                    (self.runner.job_id, task.lead_id),
                )
                if status and str(status[0][0]) == LeadStatus.queued:
                    self._queue_tasks([replace(task, attempt_id=str(uuid.uuid4()))])
        self.runner._publish(
            "lead_failed", {"lead_id": task.lead_id, "error": error_code, "counted": counted}
        )
        self.runner._publish_progress()
        self.runner._liveness.lead_ended(task.lead_id, self.runner._ts())

    async def on_activity(self, ts: float) -> None:
        self.runner._liveness.on_activity(ts)
        for event in self.runner._liveness.check(ts):
            self.runner._publish(event.kind, event.data)
        for event in self.runner._sleep.heartbeat(ts, self.runner.job_id):
            if event.kind == "sleep_detected" and self.runner._state() not in {
                JobState.paused,
                JobState.done,
                JobState.done_with_issues,
            }:
                if self.runner._pause_task is None or self.runner._pause_task.done():
                    parked = (
                        getattr(self.runner._phase_pool, "pending_pause", None)
                        if self.runner._phase_pool is not None
                        else None
                    )
                    pause_reason, pause_resume_at = parked or (PauseReason.sleep.value, None)
                    await self.runner._persist_pause(pause_reason, pause_resume_at)
                    self.runner._pause_task = asyncio.create_task(
                        self.runner._drain_persisted_pause(pause_reason, pause_resume_at)
                    )
                    self.runner._pause_task.add_done_callback(self.runner._pause_task_done)
                self.runner._liveness.sleep_detected(ts)
                start = event.data.get("start")
                if isinstance(start, (int, float)):
                    self.runner._eta.sleep_gap(float(start), ts)
                self.runner._pause_started = ts
            elif event.kind == "wake":
                if not self.runner._sleep.wait_for_network():
                    row = self.runner.deps.store.rows(
                        "SELECT pause_reason FROM jobs WHERE id=?", (self.runner.job_id,)
                    )
                    if row and row[0][0] == PauseReason.sleep.value:
                        self.runner._wake_requested = True
                        self.runner._wake_network_unavailable = True
                else:
                    row = self.runner.deps.store.rows(
                        "SELECT pause_reason FROM jobs WHERE id=?", (self.runner.job_id,)
                    )
                    if row and row[0][0] == PauseReason.sleep.value:
                        self.runner._wake_requested = True
                        self.runner._eta.pause(
                            self.runner._pause_started or ts,
                            ts,
                        )
                        self.runner._pause_started = None
                    self.runner._liveness.wake(ts)
            self.runner._publish(event.kind, event.data)

    async def on_quota(self, snapshot: QuotaSnapshot, decision: Decision) -> None:
        guard = self.runner._quota_guard
        if guard.baseline_balance is None and snapshot.credits_balance is not None:
            guard.baseline_balance = snapshot.credits_balance
            with self.runner.deps.store.transaction() as cur:
                cur.execute(
                    "UPDATE jobs SET quota_baseline=? WHERE id=?",
                    (str(snapshot.credits_balance), self.runner.job_id),
                )
        self.runner._publish("quota", {"kind": decision.kind, "reason": decision.reason})

    async def on_reroute(self, task: LeadTask, from_model: str, to_model: str, reason: str) -> None:
        attempt = self.runner._attempts.get(task.attempt_id)
        if attempt is None:
            LOGGER.warning("ignoring reroute for closed attempt %s", task.attempt_id)
            return
        self.runner.deps.store.record_reroute(attempt, from_model)
        self.runner.deps.store.record_model_used(attempt, to_model)
        self.runner.deps.store.append_event(
            self.runner.job_id,
            "reroute",
            {"lead_id": task.lead_id, "from_model": from_model, "to_model": to_model, "reason": reason},
        )
        baseline = self.runner.deps.store.rows(
            "SELECT COALESCE(MAX(ts), '') FROM job_events WHERE job_id=? AND kind='reroute_baseline'",
            (self.runner.job_id,),
        )[0][0]
        total = self.runner.deps.store.rows(
            "SELECT COUNT(DISTINCT lead_id) FROM attempts WHERE job_id=? AND ended_at IS NOT NULL "
            "AND ended_at > ?",
            (self.runner.job_id, baseline),
        )[0][0]
        rerouted = self.runner.deps.store.rows(
            "SELECT COUNT(*) FROM job_events WHERE job_id=? AND kind='reroute' "
            "AND id > COALESCE((SELECT MAX(id) FROM job_events WHERE job_id=? "
            "AND kind='reroute_baseline'), 0)",
            (self.runner.job_id, self.runner.job_id),
        )[0][0]
        if rerouted * 100 > max(1, int(total)) and self.runner._state() is not JobState.paused:
            await self.on_pause("model_unavailable", None)
        self.runner._publish("notice", {"lead_id": task.lead_id, "rerouted_to": to_model})

    async def on_pause(self, reason: str, resume_at: int | None) -> None:
        await self.runner._pause_with_pool(reason, resume_at, wait_for_phase=False)


class JobRunner:
    def __init__(self, job_id: str, deps: RunnerDeps) -> None:
        self.job_id = job_id
        self.deps = deps
        self.spec, _ = load_latest_spec(deps.job_dir)
        self._attempts: dict[str, str] = {}
        self._closed_attempts: set[str] = set()
        self._bundles: dict[str, tuple[int, None]] = {}
        self._bundle_metadata: dict[str, tuple[int, int, int]] = {}
        self._quote_failures: dict[str, tuple[dict[str, Any], list[QuoteCheck]]] = {}
        self._eta = EtaEstimator()
        self._liveness = Liveness(lambda: deps.concurrency, deps.config.limits.lead_timeout_s)
        self._sleep = SleepDetector(clock=deps.clock)
        self._masking_task: asyncio.Task[None] | None = None
        self._phase_event: asyncio.Event | None = None
        self._phase_loop: asyncio.AbstractEventLoop | None = None
        self._phase_pool: CoordinatorPool | None = None
        self._pause_started: float | None = None
        self._pause_task: asyncio.Task[None] | None = None
        row = deps.store.rows("SELECT quota_baseline FROM jobs WHERE id=?", (job_id,))
        baseline = None
        if row and row[0][0] is not None:
            from decimal import Decimal

            baseline = Decimal(str(row[0][0]))
        self._quota_guard = QuotaGuard(deps.config.limits.codex_stop_percent, baseline)
        self._phase_lock: asyncio.Lock | None = None
        self._phase_done: asyncio.Event | None = None
        self._phase_running = False
        self._pause_reason: str | None = None
        self._pause_resume_at: int | None = None
        self._pause_finished = False
        self._wake_requested = False
        self._wake_network_unavailable = False

    def _job_spec_version(self) -> int:
        rows = self.deps.store.rows("SELECT spec_version FROM jobs WHERE id=?", (self.job_id,))
        if not rows:
            raise ValueError(f"job not found: {self.job_id}")
        return int(rows[0][0])

    async def _callback_error(self, attempt_id: str) -> None:
        attempt = self._attempts.get(attempt_id)
        if attempt is not None:
            self._close_attempt(attempt_id)
            try:
                self.deps.store.fail_attempt(
                    attempt, "callback_error", True, self.deps.config.limits.lead_max_attempts
                )
            except ValueError:
                LOGGER.warning("callback error found an already closed attempt %s", attempt_id)
        self._get_phase_event().set()

    def _ts(self) -> float:
        return self.deps.clock.now() if self.deps.clock is not None else time.time()

    def _close_attempt(self, attempt_id: str) -> None:
        self._attempts.pop(attempt_id, None)
        self._closed_attempts.add(attempt_id)

    def _is_closed_attempt(self, attempt_id: str) -> bool:
        return attempt_id in self._closed_attempts

    def _drop_lead_state(self, lead_id: str) -> None:
        self._bundles.pop(lead_id, None)
        self._bundle_metadata.pop(lead_id, None)
        self._quote_failures.pop(lead_id, None)

    def _pause_task_done(self, task: asyncio.Task[None]) -> None:
        if self._pause_task is task:
            self._pause_task = None
        if not task.cancelled() and task.exception() is not None:
            LOGGER.error("scheduled pause failed", exc_info=task.exception())

    def _get_phase_event(self) -> asyncio.Event:
        loop = asyncio.get_running_loop()
        event = self._phase_event
        event_loop = getattr(cast(Any, event), "_loop", None) if event is not None else None
        if event is None or (event_loop is not None and event_loop is not loop):
            event = asyncio.Event()
            self._phase_event = event
        return event

    def _get_phase_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        lock = self._phase_lock
        lock_loop = getattr(cast(Any, lock), "_loop", None) if lock is not None else None
        if lock is None or (lock_loop is not None and lock_loop is not loop):
            lock = asyncio.Lock()
            self._phase_lock = lock
        return lock

    def _publish(self, kind: EventKind, data: dict[str, object]) -> None:
        self.deps.event_bus.publish(Event(kind, self.job_id, self._ts(), data))

    def _publish_progress(self) -> None:
        progress = counts(self.deps.store, self.job_id)
        remaining = progress.remaining
        eta = self._eta.estimate(self._ts(), remaining, self.deps.concurrency)
        self._publish(
            "counts",
            {
                "done": progress.done,
                "in_flight": progress.in_flight,
                "remaining": remaining,
                "failed": sum(progress.failed.values()),
                "held_back": progress.held_back,
            },
        )
        self._publish("eta", {"low": eta.low, "high": eta.high, "seeded": eta.seeded})

    def _state(self) -> JobState:
        rows = self.deps.store.rows("SELECT state FROM jobs WHERE id=?", (self.job_id,))
        if not rows:
            raise ValueError(f"job not found: {self.job_id}")
        return JobState(str(rows[0][0]))

    def _phase_state(self, sample: bool) -> JobState:
        return JobState.sample_running if sample else JobState.full_running

    def _base_context(self) -> MaskingContext:
        try:
            value = self.deps.masking_context(self.deps.job_dir)
        except TypeError:
            value = self.deps.masking_context()
        if not isinstance(value, MaskingContext):
            raise TypeError("masking_context must return MaskingContext")
        return value

    def _context(self) -> MaskingContext:
        value = self._base_context()
        return replace(value, harvested_names=load_harvest(self.deps.job_dir), require_harvest=True)

    def _refresh_spec(self, expected: int | None = None) -> None:
        spec, version = load_latest_spec(self.deps.job_dir)
        row = self.deps.store.rows("SELECT spec_version FROM jobs WHERE id=?", (self.job_id,))
        if not row or int(row[0][0]) != version or (expected is not None and version != expected):
            raise ValueError("latest spec does not match the job spec version")
        self.spec = spec

    def _lead_calls(self, lead_id: str, masked: bool = True) -> list[dict[str, object]]:
        path = self.deps.job_dir / ("masked" if masked else "raw") / f"{lead_id}.jsonl.gz"
        with gzip.open(path, "rt", encoding="utf-8") as source:
            return [_as_dict(json.loads(line)) for line in source if line.strip()]

    def _bundle_text(self, lead_id: str) -> str:
        bundle = build_bundle(
            lead_id,
            self._lead_calls(lead_id),
            self.spec.cap,
            self.deps.config.recipe.duration_unit,
        )
        if bundle.calls_sent == 0:
            raise ValueError("bundle has no calls under the current cap")
        return bundle.text

    def _hold_empty_bundle(self, lead_id: str, bundle: Any) -> None:
        dropped = len(bundle.calls_dropped)
        self._bundle_metadata[lead_id] = (0, dropped, bundle.chars)
        with self.deps.store.transaction() as cur:
            cur.execute(
                "UPDATE leads SET status='held_back', reason=?, calls_sent=0, calls_dropped_cap=?, chars=? "
                "WHERE job_id=? AND lead_id=?",
                (
                    "every call exceeds the per-lead cap",
                    dropped,
                    bundle.chars,
                    self.job_id,
                    lead_id,
                ),
            )

    def _prepare_leads(self, lead_ids: Iterable[str], sample: bool) -> None:
        raw_dir, masked_dir = self.deps.job_dir / "raw", self.deps.job_dir / "masked"
        for lead_id in lead_ids:
            raw, masked = raw_dir / f"{lead_id}.jsonl.gz", masked_dir / f"{lead_id}.jsonl.gz"
            try:
                context = self._context()
                stats = mask_lead_stream(raw, masked, context)
                report = scan_lead(masked, context.allow_ids)
            except Exception as exc:
                raw.unlink(missing_ok=True)
                self.deps.store.set_lead_status(
                    self.job_id,
                    lead_id,
                    LeadStatus.held_back,
                    f"masking failed: {type(exc).__name__}: {exc}",
                )
                continue
            if stats.failed or report.held_back:
                reason = stats.error or "leak scan hold-back"
                raw.unlink(missing_ok=True)
                self.deps.store.set_lead_status(self.job_id, lead_id, LeadStatus.held_back, reason)
                continue
            try:
                calls = self._lead_calls(lead_id)
                bundle = build_bundle(lead_id, calls, self.spec.cap, self.deps.config.recipe.duration_unit)
                if bundle.calls_sent == 0:
                    raw.unlink(missing_ok=True)
                    self._hold_empty_bundle(lead_id, bundle)
                    continue
                self._bundles[lead_id] = (self.spec.spec_version, None)
                self._bundle_metadata[lead_id] = (
                    bundle.calls_sent,
                    len(bundle.calls_dropped),
                    bundle.chars,
                )
                with self.deps.store.transaction() as cur:
                    cur.execute(
                        "UPDATE leads SET status='queued', calls_sent=?, calls_dropped_cap=?, chars=?, "
                        "in_sample=? WHERE job_id=? AND lead_id=?",
                        (
                            bundle.calls_sent,
                            len(bundle.calls_dropped),
                            bundle.chars,
                            int(sample),
                            self.job_id,
                            lead_id,
                        ),
                    )
                raw.unlink(missing_ok=True)
            except Exception as exc:
                raw.unlink(missing_ok=True)
                self.deps.store.set_lead_status(
                    self.job_id,
                    lead_id,
                    LeadStatus.held_back,
                    f"masking failed: {type(exc).__name__}: {exc}",
                )
                continue
            if self._phase_loop is not None:
                phase_event = self._phase_event
                if phase_event is not None:
                    self._phase_loop.call_soon_threadsafe(phase_event.set)

    async def _task_list(self, sample: bool, lead_ids: list[str]) -> list[LeadTask]:
        self._refresh_spec()
        self._load_pending_repairs()
        rows = self.deps.store.rows(
            "SELECT lead_id, reason FROM leads WHERE job_id=? AND lead_id IN ({}) ORDER BY lead_id".format(
                ",".join("?" for _ in lead_ids)
            ),
            (self.job_id, *lead_ids),
        )
        result: list[LeadTask] = []
        for lead_id in lead_ids:
            try:
                bundle = await asyncio.to_thread(
                    build_bundle,
                    lead_id,
                    self._lead_calls(lead_id),
                    self.spec.cap,
                    self.deps.config.recipe.duration_unit,
                )
                if bundle.calls_sent == 0:
                    self._hold_empty_bundle(lead_id, bundle)
                    continue
                self._bundles[lead_id] = (self.spec.spec_version, None)
                self._bundle_metadata[lead_id] = (
                    bundle.calls_sent,
                    len(bundle.calls_dropped),
                    bundle.chars,
                )
            except Exception as exc:
                self.deps.store.set_lead_status(
                    self.job_id,
                    lead_id,
                    LeadStatus.held_back,
                    f"masked bundle unavailable: {type(exc).__name__}: {exc}",
                )
                continue
            prompt = self.spec.instructions + "\n" + bundle.text
            is_repair = str(next(row[1] for row in rows if str(row[0]) == lead_id)) == "quote_repair_pending"
            if is_repair:
                answer, failures = self._quote_failures[lead_id]
                prompt = (
                    self.spec.instructions + "\n" + bundle.text + "\n" + repair_prompt(answer, failures, "")
                )
            result.append(
                LeadTask(
                    lead_id,
                    str(uuid.uuid4()),
                    prompt,
                    self.spec.output_schema,
                    self.spec.luna_model,
                    self.spec.luna_effort,
                    is_repair,
                    self.spec.spec_version,
                )
            )
        return result

    def _phase_finished(self, sample: bool) -> bool:
        if not sample and self._masking_task is not None and not self._masking_task.done():
            return False
        rows = self.deps.store.rows(
            "SELECT lead_id, status, reason FROM leads WHERE job_id=? AND in_sample=?",
            (self.job_id, int(sample)),
        )
        held: set[str] = set()
        pool = self._phase_pool
        if pool is not None:
            callbacks = getattr(pool, "callbacks", None)
            held.update(getattr(callbacks, "claimed_lead_ids", ()))
            held.update(getattr(pool, "claimed_lead_ids", ()))
        open_attempts = {
            str(row[0])
            for row in self.deps.store.rows(
                "SELECT lead_id FROM attempts WHERE job_id=? AND ended_at IS NULL", (self.job_id,)
            )
        }
        for lead_id, status, _reason in rows:
            lead_key = str(lead_id)
            if str(status) == LeadStatus.in_flight and lead_key not in open_attempts and lead_key not in held:
                LOGGER.warning("releasing orphaned claim for lead %s", lead_key)
                self.deps.store.release_claim(self.job_id, lead_key)
        rows = self.deps.store.rows(
            "SELECT status, reason FROM leads WHERE job_id=? AND in_sample=?",
            (self.job_id, int(sample)),
        )
        return not any(
            str(status) in {"pending", "pulled", "not_started", "masked", "queued", "in_flight"}
            or str(reason) == "quote_repair_pending"
            for status, reason in rows
        )

    async def _persist_pause(self, reason: str, resume_at: int | None) -> None:
        if self._pause_reason is not None and self._state() is JobState.paused:
            reason = self._pause_reason
            resume_at = self._pause_resume_at
        if reason in {item.value for item in PauseReason}:
            try:
                pause(self.deps.store, self.job_id, PauseReason(reason), resume_at)
            except TransitionError:
                if self._state() is not JobState.paused:
                    raise
                LOGGER.info("pause %s raced with another pause", reason)
            row = self.deps.store.rows("SELECT pause_reason, resume_at FROM jobs WHERE id=?", (self.job_id,))
            if row:
                reason = str(row[0][0])
                resume_at = int(row[0][1]) if row[0][1] is not None else None
        self._pause_reason = reason
        self._pause_resume_at = resume_at
        if reason != PauseReason.sleep.value:
            self._wake_requested = False
            self._wake_network_unavailable = False
        self._pause_finished = False
        if self._pause_started is None:
            self._pause_started = self._ts()

    async def _drain_persisted_pause(self, reason: str, resume_at: int | None) -> None:
        pool = self._phase_pool
        if pool is not None:
            await pool.pause(reason)
        await self._finish_pause(reason, resume_at)

    async def _finish_pause(self, reason: str, resume_at: int | None) -> None:
        if self._pause_finished:
            return
        self._pause_finished = True
        row = self.deps.store.rows("SELECT pause_reason, resume_at FROM jobs WHERE id=?", (self.job_id,))
        if row and row[0][0] is not None:
            reason = str(row[0][0])
            resume_at = int(row[0][1]) if row[0][1] is not None else None
        elif self._pause_reason is not None:
            reason = self._pause_reason
        resume_at = self._pause_resume_at if self._pause_reason is not None else resume_at
        now = self._ts()
        pause_started = self._pause_started or now
        if reason != PauseReason.model_unavailable.value:
            self._closed_attempts.update(self._attempts)
            self._attempts.clear()
            self.deps.store.requeue_in_flight(self.job_id, reason, outcome="interrupted")
        else:
            for (lead_id,) in self.deps.store.rows(
                "SELECT lead_id FROM leads WHERE job_id=? AND status='in_flight'",
                (self.job_id,),
            ):
                self.deps.store.release_claim(self.job_id, str(lead_id))
        self._get_phase_event().set()
        self._eta.pause(
            pause_started,
            float(resume_at) if resume_at is not None else now,
        )
        self._publish("pause", {"reason": reason, "resume_at": resume_at})

    async def _pause_with_pool(self, reason: str, resume_at: int | None, *, wait_for_phase: bool) -> None:
        if self._state() is JobState.paused:
            row = self.deps.store.rows("SELECT pause_reason FROM jobs WHERE id=?", (self.job_id,))
            operator_pause_reasons = {
                PauseReason.user.value,
                PauseReason.auth.value,
                PauseReason.model_unavailable.value,
                PauseReason.error_storm.value,
                PauseReason.credit.value,
                PauseReason.network.value,
                PauseReason.spend_control.value,
            }
            if (
                row
                and row[0][0] in {PauseReason.sleep.value, PauseReason.quota.value}
                and reason in operator_pause_reasons
            ):
                from bca.store import replace_pause

                replace_pause(self.deps.store, self.job_id, PauseReason(reason), None)
                self._pause_reason = None
                self._pause_resume_at = None
                self._wake_requested = False
                self._wake_network_unavailable = False
            else:
                LOGGER.info("keeping existing pause while ignoring later pause %s", reason)
            if wait_for_phase and self._phase_running and self._phase_done is not None:
                await self._phase_done.wait()
            return
        await self._persist_pause(reason, resume_at)
        pool = self._phase_pool
        if pool is not None:
            await pool.pause(reason)
            if wait_for_phase and self._phase_running:
                phase_done = self._phase_done
                if phase_done is not None:
                    await phase_done.wait()
                return
        await self._finish_pause(self._pause_reason or reason, resume_at)

    async def pause(self, reason: str) -> None:
        """Persist a pause, drain the active pool, then requeue in-flight work."""
        await self._pause_with_pool(reason, None, wait_for_phase=True)

    def _masking_needed(self, sample: bool) -> bool:
        rows = self.deps.store.rows(
            "SELECT 1 FROM leads WHERE job_id=? AND status IN ('pending', 'pulled', 'not_started') "
            "AND in_sample=? LIMIT 1",
            (self.job_id, int(sample)),
        )
        return bool(rows)

    def _start_masking_worker(self, sample: bool) -> None:
        if self._masking_task is not None and not self._masking_task.done():
            return
        lead_ids = [
            str(row[0])
            for row in self.deps.store.rows(
                "SELECT lead_id FROM leads WHERE job_id=? AND status IN ('pending', 'pulled', 'not_started') "
                "AND in_sample=? ORDER BY lead_id",
                (self.job_id, int(sample)),
            )
        ]
        if lead_ids:
            self._masking_task = asyncio.create_task(asyncio.to_thread(self._prepare_leads, lead_ids, sample))
        elif self._masking_task is not None and self._masking_task.done():
            self._masking_task = None

    async def _wait_for_masking(self, sample: bool) -> bool:
        """Wait for masking while rechecking persisted state and control state."""
        while True:
            if self._state() is JobState.paused:
                return False
            task = self._masking_task
            if task is None or task.done():
                if task is not None:
                    await asyncio.shield(task)
                    self._masking_task = None
                if not self._masking_needed(sample):
                    return True
                self._start_masking_worker(sample)
                task = self._masking_task
            if task is None:
                return not self._masking_needed(sample)
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
            except TimeoutError:
                continue
            self._masking_task = None

    def _load_pending_repairs(self) -> None:
        rows = self.deps.store.rows(
            "SELECT l.lead_id, r.answer_json, r.flags_json FROM leads l "
            "JOIN results r ON r.job_id=l.job_id AND r.lead_id=l.lead_id "
            "WHERE l.job_id=? AND l.reason='quote_repair_pending'",
            (self.job_id,),
        )
        pending_keys = {str(row[0]) for row in rows}
        for key in set(self._quote_failures) - pending_keys:
            self._quote_failures.pop(key, None)
        for lead_id, answer_json, flags_json in rows:
            key = str(lead_id)
            if key in self._quote_failures:
                continue
            answer = _as_dict(json.loads(str(answer_json)))
            flags = json.loads(str(flags_json))
            failures = [
                QuoteCheck(False, str(flag).split(":", 2)[-1], field=str(flag).split(":", 2)[1])
                for flag in flags
                if isinstance(flag, str) and flag.startswith("quote:")
            ]
            self._quote_failures[key] = (answer, failures)

    async def _run_phase(self, sample: bool, canary: bool = False) -> None:
        async with self._get_phase_lock():
            await self._run_phase_locked(sample, canary)

    async def _run_phase_locked(self, sample: bool, canary: bool = False) -> None:
        """Run one store-driven phase with one pool and one termination rule."""
        self._phase_loop = asyncio.get_running_loop()
        self._phase_running = True
        self._phase_done = asyncio.Event()
        phase_spec_version = self._job_spec_version()
        phase_event = self._get_phase_event()
        phase_event.set()
        callbacks = _Callbacks(self, sample, 1 if canary else None)
        pool = self._phase_pool
        if pool is None:
            pool = LunaPool(
                self.deps.launch_pool,
                callbacks,
                self._quota_guard,
                self.deps.concurrency,
                self.deps.config.limits.lead_timeout_s,
                retry_backoff_s=self.deps.retry_backoff_s,
                clock=self.deps.clock,
            )
            self._phase_pool = pool
        else:
            pool.callbacks = callbacks
        pool.set_concurrency(1 if canary else self.deps.concurrency)
        try:
            while True:
                await pool.run_until_idle()
                if self._job_spec_version() != phase_spec_version:
                    break
                if self._wake_requested and self._state() is JobState.paused:
                    await self._finish_pause(
                        self._pause_reason or PauseReason.sleep.value,
                        self._pause_resume_at,
                    )
                    resume_job(self.deps.store, self.job_id)
                    self._pause_started = None
                    self._pause_reason = None
                    self._pause_resume_at = None
                    self._pause_finished = False
                    if self._wake_network_unavailable:
                        await self._persist_pause(PauseReason.network.value, None)
                        await self._finish_pause(PauseReason.network.value, None)
                    self._wake_requested = False
                    self._wake_network_unavailable = False
                if self._state() is JobState.paused:
                    await self._finish_pause(
                        self._pause_reason or PauseReason.error_storm.value,
                        None,
                    )
                if self._masking_task is not None and self._masking_task.done():
                    masking_task = self._masking_task
                    self._masking_task = None
                    if masking_task.cancelled():
                        current = asyncio.current_task()
                        if current is not None and current.cancelling():
                            raise asyncio.CancelledError
                        self._start_masking_worker(sample)
                    else:
                        try:
                            masking_task.result()
                        except Exception as exc:
                            if self._state() not in {
                                JobState.paused,
                                JobState.stalled,
                                JobState.done,
                                JobState.done_with_issues,
                            }:
                                transition(
                                    self.deps.store,
                                    self.job_id,
                                    JobState.stalled,
                                    f"masking failed: {type(exc).__name__}: {exc}",
                                )
                            raise
                if self._state() is not self._phase_state(sample) or self._phase_finished(sample):
                    break
                if canary and callbacks.attempt_ended:
                    break
                phase_event = self._get_phase_event()
                phase_event.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(phase_event.wait(), 1.0)
        finally:
            self._get_phase_event().set()
            self._phase_running = False
            if self._phase_done is not None:
                self._phase_done.set()
            if not canary and self._phase_pool is pool and self._state() is not JobState.paused:
                self._phase_pool = None

    def on_plan_approved(self, expected_spec_version: int) -> None:
        if self._phase_running:
            raise TransitionError("cannot approve a plan while the previous phase is draining")
        self._wake_requested = False
        self._wake_network_unavailable = False
        self._refresh_spec(expected_spec_version)
        approve_plan(self.deps.store, self.job_id, "operator", expected_spec_version)
        transition(self.deps.store, self.job_id, JobState.pulling)
        intake = self._pull()
        if not intake.ids:
            transition(
                self.deps.store, self.job_id, JobState.stalled, "no leads resolved from the lead source"
            )
            return
        transition(self.deps.store, self.job_id, JobState.masking_sample)
        try:
            self._mask_sample()
        except Exception as exc:
            transition(
                self.deps.store,
                self.job_id,
                JobState.stalled,
                f"sample masking failed: {type(exc).__name__}: {exc}",
            )
            raise

    def _pull(self) -> IdIntake:
        source = self.spec.lead_source
        intake = resolve_lead_source(
            source,
            self.deps.bq,
            self.deps.store,
            self.deps.job_dir,
            self.deps.config.bigquery,
            self.job_id,
            self.deps.approved_bytes,
        )
        window = (
            (self.spec.window_start or "", self.spec.window_end or "")
            if self.spec.calls_scope == "window"
            else None
        )
        run_preflight(
            self.spec_to_recipe(),
            self.spec.usable,
            self.spec.calls_scope,
            window,
            intake.ids,
            self.deps.bq,
            self.deps.store,
            self.job_id,
            self.deps.config.bigquery,
            self.deps.approved_bytes,
        )
        usable_ids = [
            str(row[0])
            for row in self.deps.store.rows(
                "SELECT lead_id FROM leads WHERE job_id=? AND status='pending' ORDER BY lead_id",
                (self.job_id,),
            )
        ]
        pull_transcripts(
            self.deps.config.bigquery,
            self.spec_to_recipe(),
            usable_ids,
            self.deps.bq,
            self.deps.store,
            self.job_id,
            self.deps.job_dir,
            self.spec.calls_scope,
            window,
            self.deps.approved_bytes,
        )
        return intake

    def spec_to_recipe(self) -> Any:
        return self.deps.config.recipe

    def _build_bundles(self, lead_ids: Iterable[str]) -> None:
        for lead_id in lead_ids:
            path = self.deps.job_dir / "raw" / f"{lead_id}.jsonl.gz"
            if not path.is_file():
                continue
            self._bundles[lead_id] = (self.spec.spec_version, None)

    def _mask_sample(self) -> None:
        all_usable = [
            str(row[0])
            for row in self.deps.store.rows(
                "SELECT lead_id FROM leads WHERE job_id=? AND status IN ('pulled', 'masked') "
                "ORDER BY lead_id",
                (self.job_id,),
            )
        ]
        selected = [
            str(row[0])
            for row in self.deps.store.rows(
                "SELECT lead_id FROM leads WHERE job_id=? AND in_sample=1 "
                "AND status NOT IN ('held_back', 'failed') ORDER BY lead_id",
                (self.job_id,),
            )
        ]
        usable = list(dict.fromkeys(all_usable + selected))
        sample = selected or usable[: self.spec.sample_size]
        if sample:
            with self.deps.store.transaction() as cur:
                cur.executemany(
                    "UPDATE leads SET in_sample=1 WHERE job_id=? AND lead_id=?",
                    [(self.job_id, lead_id) for lead_id in sample],
                )
        context = self._base_context()
        harvest_path = self.deps.job_dir / "harvested_names.json"
        harvest_inputs: list[str] = []
        if context.ner is not None:

            def source_texts() -> Iterable[str]:
                for lead_id in usable:
                    raw = self.deps.job_dir / "raw" / f"{lead_id}.jsonl.gz"
                    if not raw.is_file():
                        continue
                    try:
                        calls = self._lead_calls(lead_id, False)
                    except Exception as exc:
                        self.deps.store.set_lead_status(
                            self.job_id,
                            lead_id,
                            LeadStatus.held_back,
                            f"harvest failed: {type(exc).__name__}: {exc}",
                        )
                        continue
                    harvest_inputs.append(lead_id)
                    yield from (str(record.get("transcript_text", "")) for record in calls)

            harvested = harvest_seen_twice(source_texts(), context.ner)
            usable = harvest_inputs or usable
            if harvest_inputs and (not harvest_path.exists() or not harvest_path.is_file()):
                harvest_path.write_text(
                    json.dumps(harvested, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
                )
        for lead_id in sample:
            raw_path = self.deps.job_dir / "raw" / f"{lead_id}.jsonl.gz"
            if raw_path.is_file():
                self._prepare_leads([lead_id], True)
                continue
            try:
                calls = self._lead_calls(lead_id)
                bundle = build_bundle(lead_id, calls, self.spec.cap, self.deps.config.recipe.duration_unit)
                if bundle.calls_sent == 0:
                    self._hold_empty_bundle(lead_id, bundle)
                    continue
                self._bundles[lead_id] = (self.spec.spec_version, None)
                self._bundle_metadata[lead_id] = (
                    bundle.calls_sent,
                    len(bundle.calls_dropped),
                    bundle.chars,
                )
                with self.deps.store.transaction() as cur:
                    cur.execute(
                        "UPDATE leads SET status='queued', calls_sent=?, calls_dropped_cap=?, chars=?, "
                        "in_sample=1 WHERE job_id=? AND lead_id=?",
                        (bundle.calls_sent, len(bundle.calls_dropped), bundle.chars, self.job_id, lead_id),
                    )
            except Exception as exc:
                self.deps.store.set_lead_status(
                    self.job_id,
                    lead_id,
                    LeadStatus.held_back,
                    f"masked bundle unavailable: {type(exc).__name__}: {exc}",
                )
        sample_set = set(sample)
        for lead_id in usable:
            if lead_id not in sample_set:
                status = self.deps.store.rows(
                    "SELECT status FROM leads WHERE job_id=? AND lead_id=?", (self.job_id, lead_id)
                )[0][0]
                if status in {LeadStatus.pulled, LeadStatus.masked}:
                    self.deps.store.set_lead_status(self.job_id, lead_id, LeadStatus.not_started)
        transition(self.deps.store, self.job_id, JobState.sample_running)

    async def run_sample(self) -> None:
        if self._masking_task is None:
            self._start_masking_worker(False)
        await self._run_phase(True)
        if self._state() is not JobState.sample_running:
            return
        assert not counts(self.deps.store, self.job_id).in_flight
        transition(self.deps.store, self.job_id, JobState.sample_ready)

    def on_sample_approved(self, expected_spec_version: int) -> None:
        self._refresh_spec(expected_spec_version)
        approve_sample(self.deps.store, self.job_id, "operator", expected_spec_version)

    async def start_full_run(self) -> None:
        self._refresh_spec()
        state = self._state()
        if state is JobState.sample_approved:
            start_full_run(self.deps.store, self.job_id, "operator", self.spec.spec_version)
            state = JobState.full_running
        elif state not in {
            JobState.full_running,
            JobState.quote_check,
            JobState.building_output,
            JobState.verifying,
        }:
            raise ValueError("full run requires sample approval")
        if state is not JobState.full_running:
            await self._finish_output(state)
            return
        harvest_path = self.deps.job_dir / "harvested_names.json"
        if not harvest_path.is_file():
            with self.deps.store.transaction() as cur:
                cur.execute(
                    "UPDATE leads SET status='held_back', reason=? WHERE job_id=? AND in_sample=0 "
                    "AND status IN ('pending', 'pulled', 'masked', 'queued', 'not_started')",
                    ("harvest list is missing: harvested_names.json", self.job_id),
                )
        self._start_masking_worker(False)
        await self._run_phase(False)
        if self._state() is not JobState.full_running:
            return
        assert not counts(self.deps.store, self.job_id).in_flight
        transition(self.deps.store, self.job_id, JobState.quote_check)
        await self._finish_output(JobState.quote_check)

    async def _finish_output(self, state: JobState) -> None:
        pending = self.deps.store.rows(
            "SELECT lead_id, status FROM leads WHERE job_id=? AND status IN ('queued', 'in_flight') "
            "ORDER BY lead_id",
            (self.job_id,),
        )
        if pending:
            details = ", ".join(f"{row[0]} ({row[1]})" for row in pending)
            raise RuntimeError(f"cannot build output while leads are pending: {details}")
        if state is JobState.quote_check:
            transition(self.deps.store, self.job_id, JobState.building_output)
            state = JobState.building_output
        if state is JobState.building_output:
            path = build_workbook(
                self.deps.store,
                self.job_id,
                self.spec,
                self.deps.job_dir / "output",
                self.spec.spec_version,
            )
            transition(self.deps.store, self.job_id, JobState.verifying)
        elif state is JobState.verifying:
            path = self.deps.job_dir / "output" / f"results.v{self.spec.spec_version}.xlsx"
        else:
            raise ValueError(f"cannot finish output from {state}")
        report = verify_job(self.deps.store, self.job_id, path)
        progress = counts(self.deps.store, self.job_id)
        has_issues = not report.ok or bool(progress.failed) or progress.held_back > 0
        if self._state() is JobState.verifying:
            transition(
                self.deps.store, self.job_id, JobState.done_with_issues if has_issues else JobState.done
            )

    async def resume(self, actor: Literal["operator", "automatic"] = "operator") -> None:
        """Resume a persisted pause and continue the phase recorded by lifecycle."""
        row = self.deps.store.rows("SELECT pause_reason, resume_at FROM jobs WHERE id=?", (self.job_id,))
        reason = str(row[0][0]) if row and row[0][0] is not None else ""
        resume_at = int(row[0][1]) if row and row[0][1] is not None else None
        if actor != "operator" and reason in {
            PauseReason.user.value,
            PauseReason.auth.value,
            PauseReason.model_unavailable.value,
            PauseReason.error_storm.value,
            PauseReason.credit.value,
            PauseReason.network.value,
            PauseReason.spend_control.value,
        }:
            raise PermissionError(f"{reason} pause requires operator resume")
        if actor == "automatic" and resume_at is not None and self._ts() < resume_at:
            return
        if actor == "operator" and reason == PauseReason.model_unavailable.value:
            # Reroute-rate enforcement starts a new baseline after operator recovery.
            self.deps.store.append_event(self.job_id, "reroute_baseline", {})
        # Requeue while the job is still paused so an interrupted repair stays
        # marked as an uncounted, reusable repair attempt before dispatch resumes.
        async with self._get_phase_lock():
            self._closed_attempts.update(self._attempts)
            self._attempts.clear()
            self.deps.store.requeue_in_flight(self.job_id, "resume")
            resume_job(self.deps.store, self.job_id)
            self._pause_started = None
            self._pause_reason = None
            self._pause_resume_at = None
            self._pause_finished = False
            state = self._state()
        if state is JobState.sample_running:
            await self._run_phase(True, canary=True)
            if self._state() is JobState.paused:
                return
            await self.run_sample()
        elif state is JobState.full_running:
            await self._run_phase(False, canary=True)
            if self._state() is JobState.paused:
                return
            await self.start_full_run()
        elif state in {JobState.quote_check, JobState.building_output, JobState.verifying}:
            await self._finish_output(state)

    def recover(self) -> int:
        state = self._state()
        if state is JobState.pulling:
            intake = self._pull()
            if not intake.ids:
                transition(
                    self.deps.store, self.job_id, JobState.stalled, "no leads resolved from the lead source"
                )
                return 0
            transition(self.deps.store, self.job_id, JobState.masking_sample)
            self._mask_sample()
        elif state is JobState.masking_sample:
            self._mask_sample()
        if state in {
            JobState.pulling,
            JobState.masking_sample,
            JobState.sample_running,
            JobState.full_running,
            JobState.quote_check,
            JobState.building_output,
            JobState.verifying,
        }:
            return self.deps.store.requeue_in_flight(self.job_id, "engine restart")
        return 0

    def rebuild_with_rules(self, new_rules: tuple[object, ...]) -> Path:
        from bca.outputs.excel import rebuild_with_rules as rebuild

        return rebuild(self.deps.store, self.job_id, new_rules, self.deps.job_dir / "output")
