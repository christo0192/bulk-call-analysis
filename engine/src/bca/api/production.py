from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import importlib.util
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

from bca import childenv
from bca.bq.leads import parse_id_list
from bca.claude import launch as claude_launch
from bca.claude.auth import auth_status as claude_auth_status
from bca.claude.auth import start_login as start_claude_login
from bca.claude.binary import BinaryManager, BinaryVerificationError
from bca.claude.profile import ClaudeProfile
from bca.claude.sessions import SessionStore
from bca.claude.version import PINNED_CLAUDE_VERSION
from bca.config import ConfigError, LimitsConfig, ModelsConfig, RetentionConfig, SiteConfig, load_config
from bca.coordinator.runner import Clock, JobRunner, RunnerDeps
from bca.luna.appserver import LOCKDOWN_OVERRIDES, CodexLaunch
from bca.luna.appserver import start as start_app_server
from bca.luna.quota import parse_rate_limits
from bca.luna.rpc import RpcError as AppServerRpcError
from bca.mask.masker import MaskingContext, mask_text
from bca.mask.names import SpacyNer
from bca.mcp.host import ToolHost, start_ipc_thread
from bca.mcp.ipc import new_ipc_address
from bca.monitor.events import Event, EventBus
from bca.monitor.progress import counts, progress_sample_scope
from bca.monitor.sleep import SleepDetector
from bca.paths import job_dir
from bca.setup.appdirs import directories
from bca.setup.checklist import MANUAL, Checklist
from bca.setup.spacy_model import is_model_installed
from bca.stop import StopRequested
from bca.store import (
    JobState,
    PauseReason,
    Store,
    TransitionError,
    approve_plan,
    approve_sample,
    load_latest_spec,
    open_store,
    start_full_run,
    transition,
)
from bca.store.lifecycle import public_state
from bca.store.reconcile import reconcile
from bca.store.spec import Condition, InScopeRule, JobSpec, ReviewFlagRule, ThresholdGroupRule

from .server import ApiBackend, RpcError

_SLUG = re.compile(r"^[a-z][a-z0-9-]{2,62}[a-z0-9]$")
_TOOL_SUMMARIES = {
    "bq_query": "Ran a read-only query",
    "bq_dry_run": "Checked a query",
    "bq_list_datasets": "Listed datasets",
    "bq_list_tables": "Listed tables",
    "bq_describe_table": "Described a table",
    "bq_information_schema": "Read schema information",
    "propose_job": "Proposed a job",
    "job_status": "Checked job status",
    "sample_results": "Read sample results",
    "lead_transcript": "Read a lead transcript",
    "result_aggregates": "Read result aggregates",
    "brief_read": "Read the project brief",
    "brief_update": "Updated the project brief",
    "data_notes": "Read data notes",
    "write_file": "Wrote a project file",
}


class _RealClock:
    def now(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


def _slug(value: object, field: str) -> str:
    if not isinstance(value, str) or _SLUG.fullmatch(value) is None:
        raise RpcError(-32602, "Invalid params", {"field": field, "reason": "invalid slug"})
    return value


class AppServerUnavailableError(ConnectionError):
    """The Codex app-server could not be started."""


class ClaudeTurnError(RuntimeError):
    """A Claude turn failed after the API accepted the request."""

    def __init__(
        self,
        message: str,
        reason: str = "claude_turn_failed",
        resets_at: object = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.resets_at = resets_at
        self.detail = detail


_FAILURE_TEXT = {
    "failed": "The job failed.",
    "cancelled": "The job was cancelled.",
    "pull_failed": "The job could not pull its data.",
    "background_task_failed": "The job stopped after an unexpected error.",
    "recovery_failed": "The job could not resume after the engine restarted.",
    "engine_shutdown": "The engine stopped before the job finished.",
}
_OPERATOR_WORDS = {
    "eq": "is",
    "ne": "is not",
    "in": "is one of",
    "not_in": "is not one of",
    "lt": "<",
    "le": "<=",
    "gt": ">",
    "ge": ">=",
    "is_null": "is empty",
    "not_null": "is not empty",
    "contains": "contains",
}
# A failed chat turn is stored in history.jsonl; keep its text to fixed words, never the raw error.
_TURN_FAILED_TEXT = {
    "claude_turn_failed": "Turn failed: Claude could not finish this reply.",
    "claude_limit_reached": "Turn failed: the Claude allowance is used up for now.",
    "claude_extra_usage_on": "Turn failed: Claude extra usage is turned on.",
}
_STALL_TEXT = {
    "pull_failed": "Pulling the transcripts failed.",
    "recovery_failed": "The job could not recover after a restart.",
    "masking_failed": "Masking the transcripts failed.",
    "no leads resolved from the lead source": "No leads matched the lead source.",
    "background task failed": "A background step failed.",
}


def _safe_error(exc: Exception) -> RpcError:
    if isinstance(exc, RpcError):
        return exc
    logging.getLogger("bca.api").exception("Production API failure")
    if isinstance(exc, ConfigError):
        detail = str(exc).split(": ", 1)
        key = detail[0].split("\\")[-1].split("/")[-1]
        reason = detail[1] if len(detail) == 2 else "invalid configuration"
        return RpcError(-32602, "Invalid configuration", {"field": key, "reason": reason})
    if isinstance(exc, AppServerUnavailableError):
        return RpcError(-32001, "Codex app-server did not start", {"reason": "app_server_unavailable"})
    if isinstance(exc, ClaudeTurnError):
        data: dict[str, object] = {"reason": exc.reason, "message": str(exc)}
        if exc.detail is not None:
            data["detail"] = exc.detail
        if exc.resets_at is not None:
            data["resetsAt"] = exc.resets_at
        return RpcError(-32001, "Claude turn failed", data)
    if isinstance(exc, OSError):
        return RpcError(-32001, "Operation failed", {"reason": "filesystem_unavailable"})
    return RpcError(-32603, "Internal error")


def _config_detail(exc: ConfigError) -> str:
    text = str(exc)
    for marker in (": invalid TOML", ": cannot read configuration"):
        if marker in text:
            return f"configuration: {marker[2:]}"
    if ": " not in text:
        return "configuration: invalid configuration"
    key, reason = text.split(": ", 1)
    return f"{key}: {reason}"


class JobManager:
    def __init__(self, backend: ProductionBackend) -> None:
        self.backend = backend
        self.runners: dict[str, JobRunner] = {}
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self._recovered: set[str] = set()
        self._recovery_scheduled = False
        self._recovery_tasks: set[asyncio.Task[None]] = set()

    def runner(self, job_id: str) -> JobRunner:
        runner = self.runners.get(job_id)
        if runner is None:
            row = self.backend.store.rows("SELECT project_id FROM jobs WHERE id=?", (job_id,))
            if not row:
                raise RpcError(-32001, "Job not found", {"job": job_id})
            runner = JobRunner(job_id, self.backend.runner_deps(job_id, str(row[0][0])))
            self.runners[job_id] = runner
        return runner

    def recover(self) -> None:
        running = {
            JobState.plan_approved,
            JobState.pulling,
            JobState.masking_sample,
            JobState.sample_running,
            JobState.full_running,
            JobState.quote_check,
            JobState.building_output,
            JobState.verifying,
        }
        for row in self.backend.store.rows("SELECT id, state FROM jobs"):
            if JobState(str(row[1])) in running:
                job_id = str(row[0])
                task = self.tasks.get(job_id)
                if task is not None and not task.done():
                    continue
                self._recovered.add(job_id)

    def schedule_recovery(self) -> None:
        if self._recovery_scheduled or not self._recovered:
            return
        self._recovery_scheduled = True
        for job_id in tuple(self._recovered):
            task = asyncio.create_task(self._recover_one(job_id))
            self._recovery_tasks.add(task)
            task.add_done_callback(self._recovery_tasks.discard)
        self._recovered.clear()

    async def wait_for_recovery(self) -> None:
        tasks = tuple(self._recovery_tasks)
        if tasks:
            await asyncio.gather(*tasks)

    async def _recover_one(self, job_id: str) -> None:
        async def operation(runner: JobRunner) -> None:
            try:
                await runner._worker(runner.recover)
                current = runner._state()
                if current is JobState.paused:
                    return
                if current in {JobState.plan_approved, JobState.pulling, JobState.masking_sample}:
                    await runner._worker(runner.prepare_sample)
                    if runner._state() is JobState.sample_running:
                        await runner.run_sample()
                elif current is JobState.sample_running:
                    await runner.run_sample()
                elif current is not JobState.stalled:
                    await runner.start_full_run()
            except StopRequested:
                return
            except Exception:
                if runner._state() is not JobState.stalled:
                    transition(self.backend.store, job_id, JobState.stalled, "recovery_failed")
                return

        try:
            await self.start(job_id, operation)
            task = self.tasks.get(job_id)
            if task is not None:
                await asyncio.shield(task)
        except Exception:
            logging.getLogger("bca.api").exception("Recovery failed for job %s", job_id)
            with contextlib.suppress(Exception):
                if self.backend.store.rows("SELECT state FROM jobs WHERE id=?", (job_id,)):
                    transition(self.backend.store, job_id, JobState.stalled, "recovery_failed")

    async def start(self, job_id: str, operation: Callable[[JobRunner], Awaitable[None]]) -> None:
        old = self.tasks.get(job_id)
        if old is not None and not old.done():
            raise RpcError(-32001, "Job is already running", {"job": job_id, "reason": "job_running"})

        async def invoke() -> None:
            rows = self.backend.store.rows("SELECT project_id FROM jobs WHERE id=?", (job_id,))
            if not rows:
                raise RpcError(-32001, "Job not found", {"job": job_id})
            runner = self.runner(job_id)
            await runner.wait_for_workers()
            if self.backend._stopping:
                return
            runner.clear_stop()
            try:
                if self.backend._stopping:
                    return
                await operation(runner)
            except StopRequested:
                return

        task = asyncio.create_task(invoke())
        self.tasks[job_id] = task

        def finished(done: asyncio.Task[None]) -> None:
            if done.cancelled() or done.exception() is None:
                return
            error = done.exception()
            assert error is not None
            logging.getLogger("bca.api").error(
                "Job %s failed", job_id, exc_info=(type(error), error, error.__traceback__)
            )
            try:
                transition(self.backend.store, job_id, JobState.stalled, "background task failed")
            except Exception:
                logging.getLogger("bca.api").exception("Could not stall failed job %s", job_id)
            self.backend.event_bus.publish(
                Event("stall", job_id, self.backend.clock.now(), {"reason": "background_task_failed"})
            )

        task.add_done_callback(finished)

    async def stop(self, job_id: str, timeout: float = 10.0) -> None:
        runner = self.runners.get(job_id)
        if runner is not None:
            runner.request_stop()
        task = self.tasks.get(job_id)
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout)
            except TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            except StopRequested:
                pass
            except asyncio.CancelledError:
                if not task.cancelled():
                    raise
        self.tasks.pop(job_id, None)

    async def pause(self, job_id: str, timeout: float = 1.0) -> None:
        runner = self.runner(job_id)
        runner.request_stop_for_prepare()
        await runner.pause(PauseReason.user.value)
        task = self.tasks.get(job_id)
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout)
            except TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            except Exception:
                logging.getLogger("bca.api").exception("Paused job driver failed for %s", job_id)
        self.tasks.pop(job_id, None)

    async def shutdown(self) -> None:
        for project in list(self.backend._chat_tasks):
            await self.backend._chat_stop(project)
        # Let already-scheduled recovery wrappers create their drivers so the
        # same stop-and-wait path covers startup recovery as well.
        await asyncio.sleep(0)
        for runner in self.runners.values():
            runner.request_stop()
        for job_id in list(self.tasks):
            await self.stop(job_id, timeout=1.0)
        for runner in self.runners.values():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(runner.wait_for_workers(), 5.0)
        recovery_tasks = tuple(self._recovery_tasks)
        for task in recovery_tasks:
            if not task.done():
                task.cancel()
        if recovery_tasks:
            await asyncio.gather(*recovery_tasks, return_exceptions=True)
        for row in self.backend.store.rows(
            "SELECT id FROM jobs WHERE state IN (?, ?, ?, ?, ?, ?, ?)",
            tuple(
                state.value
                for state in (
                    JobState.pulling,
                    JobState.masking_sample,
                    JobState.sample_running,
                    JobState.full_running,
                    JobState.quote_check,
                    JobState.building_output,
                    JobState.verifying,
                )
            ),
        ):
            self.backend.store.requeue_in_flight(str(row[0]), "engine shutdown")
        for process in tuple(self.backend._login_processes):
            if process.poll() is None:
                with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                    if os.name == "nt":
                        subprocess.run(
                            ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                            check=False,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                    else:
                        process.terminate()
                    process.wait(timeout=1)
                if process.poll() is None:
                    with contextlib.suppress(OSError):
                        process.kill()
            self.backend._login_processes.discard(process)
            self.backend._codex_logins.discard(process)


# npm installs Codex as a `codex.cmd` shim next to its package; the shim's codex.js only finds
# and starts this native program, which the engine can start directly.
_NPM_CODEX_NATIVE = {
    "AMD64": ("@openai/codex-win32-x64", "x86_64-pc-windows-msvc"),
    "ARM64": ("@openai/codex-win32-arm64", "aarch64-pc-windows-msvc"),
}


def _npm_native_codex(shim_dir: Path) -> Path | None:
    import platform

    package, triple = _NPM_CODEX_NATIVE.get(platform.machine().upper(), _NPM_CODEX_NATIVE["AMD64"])
    root = shim_dir / "node_modules" / "@openai" / "codex"
    for vendor in (root / "node_modules" / package / "vendor", root / "vendor"):
        native = vendor / triple / "bin" / "codex.exe"
        if native.is_file():
            return native.resolve()
    return None


def _on_path_exactly(file_name: str) -> Path | None:
    """The first PATH folder holding exactly `file_name`. Unlike shutil.which, no PATHEXT is tried
    (it would accept `claude.exe.cmd`) and the current folder is not searched."""
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        folder = folder.strip().strip('"')
        if not folder:
            continue
        try:
            if (Path(folder) / file_name).is_file():
                return (Path(folder) / file_name).absolute()
        except OSError:
            continue  # an unreadable folder or file: look further along PATH
    return None


def _missing_program(stem: str) -> Path:
    """An absolute path that does not exist, for a program that was not found. Given a bare name,
    Windows' CreateProcess would search the current folder (and add .exe) where our lookup did not,
    so the launch could start a program the setup check said was missing."""
    return Path(tempfile.gettempdir()).absolute() / "bca-program-not-found" / f"{stem}.exe"


def _program(name: str) -> Path:
    """The program file to launch for `name` (a bare name, or a path from BCA_*_BIN).

    The lookup never picks a `.cmd` or `.bat` on its own: Windows runs those through cmd.exe, where
    text on the command line (a data folder named like "R&D") would be read as commands and could
    cut off the flags that keep Claude contained. So on Windows a bare name finds exactly
    `<name>.exe` in a PATH folder; when only npm's `codex.cmd` shim is there, the native
    `codex.exe` npm installed with it is used instead. A configured path without an extension gets
    `.exe` (npm's extensionless `codex` is a shell script Windows cannot start). A `.cmd` or `.bat`
    someone set on purpose in BCA_CLAUDE_BIN or BCA_CODEX_BIN is used as given (the test launchers
    are .cmd files). Elsewhere a bare name is looked up on PATH. On Windows whatever comes back
    is an absolute path, the startable file or one that does not exist: never a bare name, which
    CreateProcess would search for in the current folder. So the setup check (is_file) and the
    launch always agree.
    """
    path = Path(name)
    bare = path.parent == Path(".")
    if os.name != "nt":
        found = shutil.which(name) if bare else (name if path.is_file() else None)
        return Path(found).absolute() if found is not None else path
    suffix = path.suffix.lower()
    stem = path.with_suffix("") if suffix in {".exe", ".cmd", ".bat"} else path
    is_codex = stem.name.lower() == "codex"
    if bare:
        exe_on_path = _on_path_exactly(stem.name + ".exe")
        if exe_on_path is not None:
            return exe_on_path
        shim = _on_path_exactly(stem.name + ".cmd")
        native = _npm_native_codex(shim.parent) if shim is not None and is_codex else None
        return native if native is not None else _missing_program(stem.name)
    if suffix == ".exe":
        return path.absolute()
    if suffix in {".cmd", ".bat"}:
        native = _npm_native_codex(path.parent) if is_codex else None
        return native if native is not None else path.absolute()
    executable = stem.with_name(stem.name + ".exe").absolute()
    if executable.is_file():
        return executable
    if is_codex and stem.with_name(stem.name + ".cmd").is_file():
        native = _npm_native_codex(stem.parent)
        if native is not None:
            return native
    # Nothing Windows can start: the missing .exe (absolute, so no search), so the check and the
    # launch both say not found.
    return executable


class ProductionBackend(ApiBackend):
    """API backend backed by the persistent store and the real coordinator."""

    def __init__(
        self,
        app_data: Path | None = None,
        *,
        config: SiteConfig | None = None,
        store: Store | None = None,
        bq: Any | None = None,
        launch_pool: Callable[[], Awaitable[Any]] | None = None,
        masking_context: Callable[..., MaskingContext] | None = None,
        clock: Clock | None = None,
        event_bus: EventBus | None = None,
        ner: Any | None = None,
        concurrency: int | None = None,
    ) -> None:
        configured_data_dir = os.environ.get("BCA_DATA_DIR")
        self.app_data = (
            app_data or (Path(configured_data_dir) if configured_data_dir else directories().app_data)
        ).expanduser()
        self.app_data.mkdir(parents=True, exist_ok=True)
        # An explicit argument wins; otherwise the worker count saved in Settings survives a restart.
        self.concurrency = concurrency if concurrency is not None else self._saved_worker_count()
        self.config_path = self.app_data / "site.toml"
        self._config_supplied = config is not None
        self.config: SiteConfig = cast(SiteConfig, config)
        self.config_error: str | None = None
        self._recovered = False
        if config is None:
            self._reload_config()
        self.store = store or open_store(self.app_data / "store.sqlite3")
        self.event_bus = event_bus or EventBus()
        self.clock = clock or _RealClock()
        self.store.transition_listener = self._publish_transition
        self._provided_bq, self._provided_launch, self._provided_masker = bq, launch_pool, masking_context
        self._provided_ner = ner
        self._bq = bq
        self._masker = masking_context
        self._ner = ner
        self.sleep = SleepDetector(clock=self.clock)
        self.power_status: dict[str, bool | int | None] = {"onAc": None, "percent": None}
        self.jobs = JobManager(self)
        self._chat_tasks: dict[str, asyncio.Task[dict[str, object]]] = {}
        self._chat_sessions: dict[str, claude_launch.SessionRef] = {}
        self._chat_mcp_configs: dict[str, Path] = {}
        self._chat_stop_requests: set[asyncio.Task[dict[str, object]]] = set()
        self._chat_turns: dict[str, str] = {}
        self._chat_entry_ids: dict[str, int] = {}
        self._chat_notify: Callable[[str, dict[str, object]], Awaitable[None]] | None = None
        self._login_processes: set[subprocess.Popen[str]] = set()
        self._codex_logins: set[subprocess.Popen[str]] = set()
        # None as the value means signed out: cached too, so a signed-out PC does not start Codex for
        # every status call.
        self._codex_allowance_cache: tuple[float, dict[str, object] | None] | None = None
        self._codex_allowance_locks: dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}
        self._chat_sessions_store = SessionStore(self.app_data / "claude" / "sessions.json")
        self._closed = False
        self._stopping = False
        self._resume_watcher_task: asyncio.Task[None] | None = None
        if self.config is not None:
            self.jobs.recover()
            self._recovered = True

    def _reload_config(self) -> None:
        if self._config_supplied:
            return
        if not self.config_path.is_file():
            self.config = cast(SiteConfig, None)
            self.config_error = None
            return
        try:
            self.config = load_config(self.config_path)
        except ConfigError as exc:
            logging.getLogger("bca.api").exception("Configuration load failed")
            self.config = cast(SiteConfig, None)
            self.config_error = _config_detail(exc)
        else:
            self.config_error = None
            if hasattr(self, "jobs") and not self._recovered:
                self.jobs.recover()
                self._recovered = True

    def _setup_probes(self) -> dict[str, Callable[[], tuple[bool, str]]]:
        """First-run checks; each detail is plain words that match the result (shown as is)."""
        config = self.config

        def connector() -> tuple[bool, str]:
            if config is None and self.config_error is not None:
                return False, f"The settings file {self.config_path} could not be read: {self.config_error}"
            if config is None:
                return False, f"BigQuery settings not found; put site.toml in {self.app_data}."
            if config.bigquery.key_path is None:
                return False, "The settings file names no BigQuery key file (key_path)."
            return True, "BigQuery settings found."

        def key() -> tuple[bool, str]:
            if config is None or config.bigquery.key_path is None:
                return False, "Waiting for the BigQuery settings."
            if config.bigquery.key_path.is_dir():
                return False, f"The BigQuery key path is a folder, not a file: {config.bigquery.key_path}"
            if not config.bigquery.key_path.is_file():
                # The path may be shown; the key's contents never are (D-UI-KEYPATH).
                return False, f"The BigQuery key file is missing: {config.bigquery.key_path}"
            return True, "BigQuery key file found."

        def spacy() -> tuple[bool, str]:
            if self._test_fakes_allowed():
                return True, "Name-masking model installed."
            # The model wheel does not depend on spaCy, so each must be checked: with the model
            # alone, the first job would fail when the masker loads spaCy.
            if importlib.util.find_spec("spacy") is None:
                return False, "spaCy is not installed. Run the app's start script again."
            if not is_model_installed():
                return False, "The name-masking model is not installed. Run the app's start script again."
            return True, "Name-masking model installed."

        def claude_copy() -> tuple[bool, str]:
            if self._test_fakes_allowed():
                return True, "available"
            path, detail = self._claude_program()
            return path.is_file(), detail

        def codex_copy() -> tuple[bool, str]:
            # The exact file the launch will start (see _program), so the two cannot disagree.
            if self._codex_binary().is_file():
                return True, "Codex program found."
            return False, "The Codex program was not found. Install it, then press Check again."

        return {
            "uv_python": lambda: (sys.version_info >= (3, 12), sys.version.split()[0]),
            "venv": lambda: (True, sys.executable),
            "connector": connector,
            "key": key,
            "claude_copy": claude_copy,
            "codex_copy": codex_copy,
            "spacy": spacy,
        }

    def _start_resume_watcher(self) -> None:
        if self._resume_watcher_task is None or self._resume_watcher_task.done():
            self._resume_watcher_task = asyncio.create_task(self._resume_watcher(), name="bca-quota-resume")

    async def _resume_watcher(self) -> None:
        while not self._stopping:
            try:
                sleep = getattr(self.clock, "sleep", None)
                if callable(sleep):
                    await sleep(30)
                else:
                    await asyncio.sleep(30)
                now = self.clock.now()
                rows = self.store.rows(
                    "SELECT id, resume_at FROM jobs WHERE state='paused' AND pause_reason=? "
                    "AND resume_at IS NOT NULL AND resume_at<=?",
                    (PauseReason.quota.value, int(now)),
                )
                for row in rows:
                    job_id, due_at = str(row[0]), int(row[1])
                    task = self.jobs.tasks.get(job_id)
                    if self._stopping or (task is not None and not task.done()):
                        continue

                    async def automatic(
                        runner: JobRunner, job_id: str = job_id, due_at: int = due_at
                    ) -> None:
                        try:
                            await runner.resume("automatic")
                        except (PermissionError, TransitionError, RuntimeError):
                            retry_at = int(self.clock.now() + 600)
                            with self.store.transaction() as cur:
                                cur.execute(
                                    "UPDATE jobs SET resume_at=?, updated_at=? "
                                    "WHERE id=? AND state='paused' AND pause_reason='quota' AND resume_at=?",
                                    (retry_at, datetime.now(UTC).isoformat(), job_id, due_at),
                                )
                                refused = cur.rowcount == 1
                            if not refused:
                                # The job left this quota pause, so the resumed run raised: let the
                                # job manager stall it, as it does for an operator resume.
                                raise

                    try:
                        await self.jobs.start(job_id, automatic)
                    except Exception:
                        logging.getLogger("bca.api").exception("Quota resume failed for job %s", job_id)
            except asyncio.CancelledError:
                return
            except Exception:
                logging.getLogger("bca.api").exception("Quota resume watcher failed")

    def _require_config(self) -> SiteConfig:
        self._reload_config()
        if self.config is None:
            data: dict[str, object] = {"reason": "setup_required"}
            if self.config_error is not None:
                data["message"] = self.config_error
            raise RpcError(-32002, "Setup required", data)
        return self.config

    def set_chat_notify(self, callback: Callable[[str, dict[str, object]], Awaitable[None]]) -> None:
        self._chat_notify = callback

    def _test_fakes_allowed(self) -> bool:
        return os.environ.get("BCA_TEST_FAKES") == "1" and (self.app_data / ".bca-test-fakes").is_file()

    def _make_bq(self) -> Any:
        if self._bq is not None:
            return self._bq
        if self._test_fakes_allowed():
            from tests.fakes.fake_bq import FakeBqRunner

            fake_data = self.app_data / "fake-bq.json"
            if fake_data.is_file():
                return FakeBqRunner.from_file(fake_data)
            return FakeBqRunner()
        from bca.bq.runner import GoogleBqRunner

        return GoogleBqRunner(self.config.bigquery)

    async def _launch(self, job_dir: Path) -> Any:
        job_dir.mkdir(parents=True, exist_ok=True)
        if self._provided_launch is not None:
            return await self._provided_launch()
        try:
            if self._test_fakes_allowed():
                from tests.fakes import fake_codex

                scenario = job_dir / "fake-codex-scenario.json"
                # Launch the fake by file path: a `-m tests.fakes...` launch cannot import `tests`
                # from the job directory with the scrubbed environment.
                command = (sys.executable, str(Path(fake_codex.__file__)), "--scenario", str(scenario))
                return await start_app_server(CodexLaunch(command, self.app_data / "codex", job_dir))
            return await start_app_server(CodexLaunch(self._codex_binary(), self.app_data / "codex", job_dir))
        except (OSError, ConnectionError, RuntimeError) as exc:
            raise AppServerUnavailableError("Codex app-server did not start") from exc

    def _make_masker(self) -> Callable[..., MaskingContext]:
        if self._masker is not None:
            return self._masker
        if self._test_fakes_allowed():

            class FakeNer:
                def persons(self, text: str) -> list[Any]:
                    return []

            self._ner = FakeNer()
        else:
            self._ner = self._ner or SpacyNer()
        return lambda *_: MaskingContext(roster_file=self.config.masking.roster_file, ner=self._ner)

    def runner_deps(self, job_id: str, project_id: str) -> RunnerDeps:
        job_path = job_dir(self.app_data, project_id, job_id)
        job_path.mkdir(parents=True, exist_ok=True)
        return RunnerDeps(
            self.store,
            self._make_bq(),
            lambda *_: self._make_masker()(job_path),
            lambda: self._launch(job_path),
            self.clock,
            self.event_bus,
            self.config,
            job_path,
            self._ner,
            self.concurrency,
            approved_bytes=None,
            can_dispatch=self._can_dispatch,
        )

    def _can_dispatch(self) -> bool:
        return not (
            self.power_status["onAc"] is not True
            and isinstance(self.power_status["percent"], int)
            and self.power_status["percent"] < 20
        )

    def _claude_profile(self) -> ClaudeProfile:
        profile = ClaudeProfile(
            self.app_data / "claude",
            self.app_data / "claude-work",
            configured_model=self.config.models.claude_model if self.config else ModelsConfig().claude_model,
        )
        profile.write_settings()
        return profile

    def _claude_binary(self) -> Path:
        return self._claude_program()[0]

    def _claude_program(self) -> tuple[Path, str]:
        """The Claude Code program every launch uses, and a plain-words status for the setup row.

        The app runs its own verified copy of the pinned version, made once from the official
        install on this PC, so Claude's own updates cannot change the version the turn guard
        accepts. BCA_CLAUDE_BIN names another program (tests and development).
        """
        configured = os.environ.get("BCA_CLAUDE_BIN")
        if configured:
            path = _program(configured)
            found = path.is_file()
            return path, "Claude Code found." if found else f"The Claude Code program was not found: {path}"
        manager = BinaryManager(self.app_data / "programs", PINNED_CLAUDE_VERSION)
        ready = f"Claude Code {PINNED_CLAUDE_VERSION} ready."
        try:
            return manager.path(), ready
        except BinaryVerificationError:
            pass
        try:
            return manager.install_from_local(), ready
        except (BinaryVerificationError, OSError) as exc:
            logging.getLogger("bca.api").warning(
                "Claude Code %s is not available: %s", PINNED_CLAUDE_VERSION, exc
            )
            return manager.destination / manager._filename(), (
                f"The app runs Claude Code {PINNED_CLAUDE_VERSION}, which was not found on this PC. "
                f"Install it with: claude install {PINNED_CLAUDE_VERSION}. Then press Check again."
            )

    def _codex_binary(self) -> Path:
        return _program(os.environ.get("BCA_CODEX_BIN", "codex"))

    def _project_dir(self, project: str) -> Path:
        rows = self.store.rows("SELECT 1 FROM projects WHERE id=?", (project,))
        if not rows:
            raise RpcError(-32001, "Project not found", {"project": project, "reason": "project_not_found"})
        path = (self.app_data / "projects" / project).resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _settings_path(self) -> Path:
        return self.app_data / "settings.json"

    def _saved_settings(self) -> dict[str, object]:
        try:
            value = json.loads(self._settings_path().read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _positive_int(value: object, upper: int | None = None) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            return None
        return value if upper is None or value <= upper else None

    def _saved_worker_count(self) -> int:
        return self._positive_int(self._saved_settings().get("workerCount")) or 4

    def _retention(self) -> RetentionConfig:
        """The retention every consumer applies: the days saved in Settings, else the site config."""
        base = self.config.retention if self.config is not None else RetentionConfig()
        days = self._positive_int(self._saved_settings().get("retentionDays"), 3650)
        if days is None:
            return base
        # Settings says "keep masked transcripts and chats for N days": one number for both.
        return dataclasses.replace(base, masked_cache_days=days, chat_days=days)

    def _save_settings(self, values: dict[str, object]) -> None:
        path = self._settings_path()
        fd, name = tempfile.mkstemp(prefix=".settings-", dir=self.app_data)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(values, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)

    def _chat_history_path(self, project: str) -> Path:
        path = self._project_dir(project) / "chat" / "history.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _append_chat_entry(self, project: str, entry: dict[str, object]) -> int:
        path = self._chat_history_path(project)
        last = self._chat_entry_ids.get(project, 0)
        if last == 0 and path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict) and isinstance(value.get("id"), int):
                    last = max(last, int(value["id"]))
        next_id = last + 1
        value = {"id": next_id, "ts": int(self.clock.now()), **entry}
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
        self._chat_entry_ids[project] = next_id
        return next_id

    @staticmethod
    def _file_kind(name: str) -> str:
        lowered = name.lower()
        if "sample" in lowered:
            return "sample"
        if "result" in lowered or lowered.endswith(".xlsx"):
            return "results"
        if "summary" in lowered:
            return "summary"
        if "report" in lowered:
            return "report"
        if "plan" in lowered or lowered.startswith("spec."):
            return "plan"
        if "brief" in lowered:
            return "brief"
        if "sample" in lowered:
            return "sample"
        return "other"

    @staticmethod
    def _masked(value: object, context: MaskingContext) -> object:
        if isinstance(value, str):
            return mask_text(value, context).text
        if isinstance(value, dict):
            structural = {"session_id", "sessionId", "tool_use_id", "toolUseId", "attempt_id", "attemptId"}
            return {
                str(key): item if str(key) in structural else ProductionBackend._masked(item, context)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [ProductionBackend._masked(item, context) for item in value]
        return value

    async def _chat_turn(
        self,
        project: str,
        text_value: str,
        session_override: claude_launch.SessionRef | None = None,
        reseeded: bool = False,
    ) -> dict[str, object]:
        project_dir = self._project_dir(project)
        turn_id = self._chat_turns.get(project, str(uuid4()))
        context = self._make_masker()(project_dir)
        masked_text = cast(str, self._masked(text_value, context))
        self._append_chat_entry(project, {"turn": turn_id, "role": "user", "text": masked_text})
        tool_names: dict[str, str] = {}
        last_tool_name = "tool"
        address, _family = new_ipc_address()
        authkey = os.urandom(32)
        host = ToolHost(
            runner=self._make_bq(),
            store=self.store,
            project_dir=project_dir,
            bq_config=self.config.bigquery,
            mask_context=context,
            project_id=project,
            recipe=self.config.recipe,
            models=self.config.models,
        )
        chat_dir = project_dir / "chat"
        chat_dir.mkdir(parents=True, exist_ok=True)
        profile = self._claude_profile()
        brief_path = project_dir / "brief.md"
        brief = brief_path.read_text(encoding="utf-8") if brief_path.is_file() else ""
        brief = mask_text(brief, context).text
        playbook_text = (
            "You are assisting with the selected project. When the operator attaches an id list, "
            "their message names the file (for example ids-3.txt). Use a lead source of kind ids "
            "with that file name.\n"
        )
        playbook_version = hashlib.sha256(playbook_text.encode("utf-8")).hexdigest()[:16]
        playbook = chat_dir / f"playbook-{playbook_version}.md"
        playbook.write_text(playbook_text, encoding="utf-8", newline="\n")
        choice = self._chat_sessions_store.get(project, playbook_version)
        if choice.status == "needs_choice":
            choice = self._chat_sessions_store.resume_or_reseed(project, playbook_version, False)
        if session_override is not None:
            session = session_override
        elif choice.status == "new":
            session = claude_launch.SessionRef(uuid4())
        else:
            assert choice.session is not None
            session = claude_launch.SessionRef(choice.session, resumed=True)
        self._chat_sessions[project] = session
        mcp_config = chat_dir / "mcp.json"
        mcp_config.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "bq": {
                            "command": sys.executable,
                            "args": [
                                "-m",
                                "bca.mcp.bq_server",
                                "--engine-socket",
                                address,
                                "--project",
                                project,
                            ],
                            "env": {"BCA_MCP_AUTHKEY": authkey.hex()},
                        },
                        "app": {
                            "command": sys.executable,
                            "args": [
                                "-m",
                                "bca.mcp.app_server",
                                "--engine-socket",
                                address,
                                "--project",
                                project,
                            ],
                            "env": {"BCA_MCP_AUTHKEY": authkey.hex()},
                        },
                    }
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        self._chat_mcp_configs[project] = mcp_config
        try:
            start_ipc_thread(host, address, authkey)
            async for event in claude_launch.run_turn(
                self._claude_binary(),
                profile,
                mcp_config,
                playbook,
                profile.settings_path,
                f"You are assisting with the selected project.\n\nBrief:\n{brief}",
                masked_text,
                self.config.models.claude_model,
                self.config.models.claude_effort,
                self.config.limits.claude_turn_max_turns,
                session,
            ):
                if event.kind == "system" and event.data.get("subtype") == "init" and not session.resumed:
                    self._chat_sessions_store.save(project, session.session_id, playbook_version)
                safe_event = cast(dict[str, object], self._masked(event.data, context))
                if event.kind == "assistant":
                    text = safe_event.get("result") or safe_event.get("text")
                    if not isinstance(text, str):
                        message = safe_event.get("message")
                        content = message.get("content") if isinstance(message, dict) else None
                        if isinstance(content, list):
                            text = "".join(
                                str(block["text"])
                                for block in content
                                if isinstance(block, dict)
                                and block.get("type") == "text"
                                and isinstance(block.get("text"), str)
                            )
                    if isinstance(text, str):
                        self._append_chat_entry(project, {"turn": turn_id, "role": "assistant", "text": text})
                elif event.kind in {"tool_use", "tool_result"}:
                    message = safe_event.get("message")
                    content = message.get("content") if isinstance(message, dict) else None
                    block = (
                        next(
                            (item for item in content if isinstance(item, dict)),
                            {},
                        )
                        if isinstance(content, list)
                        else {}
                    )
                    tool_id = str(block.get("id") or block.get("tool_use_id") or "")
                    tool_name = str(
                        block.get("name")
                        or safe_event.get("name")
                        or safe_event.get("tool_name")
                        or tool_names.get(tool_id, last_tool_name)
                    )
                    if event.kind == "tool_use" and tool_id:
                        tool_names[tool_id] = tool_name
                    if event.kind == "tool_use":
                        last_tool_name = tool_name
                    for prefix in ("mcp__app__", "mcp__bq__"):
                        tool_name = tool_name.removeprefix(prefix)
                    failed = bool(
                        block.get("is_error")
                        or block.get("isError")
                        or safe_event.get("is_error")
                        or safe_event.get("isError")
                    )
                    status = "started" if event.kind == "tool_use" else ("failed" if failed else "done")
                    summary = _TOOL_SUMMARIES.get(tool_name, f"Ran {tool_name}")
                    self._append_chat_entry(
                        project,
                        {
                            "turn": turn_id,
                            "role": "tool",
                            "name": tool_name,
                            "status": status,
                            "summary": summary,
                        },
                    )
                elif event.kind == "rate_limit":
                    allowance = {
                        "checkedAt": int(self.clock.now()),
                        "rateLimitInfo": safe_event.get("rate_limit_info"),
                    }
                    path = self.app_data / "claude" / "allowance.json"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    temporary = path.with_name(".allowance.json.tmp")
                    temporary.write_text(
                        json.dumps(allowance, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
                    )
                    temporary.replace(path)
                entry_id = self._chat_entry_ids.get(project, 0)
                if self._chat_notify is not None:
                    await self._chat_notify(
                        project,
                        {"kind": event.kind, "data": safe_event, "turn": turn_id, "entryId": entry_id},
                    )
            if asyncio.current_task() in self._chat_stop_requests:
                return {"status": "stopped", "project": project}
            return {"status": "completed", "project": project}
        except asyncio.CancelledError:
            if asyncio.current_task() in self._chat_stop_requests:
                return {"status": "stopped", "project": project}
            raise
        except (ClaudeTurnError, claude_launch.TurnGuardError) as exc:
            if asyncio.current_task() in self._chat_stop_requests:
                return {"status": "stopped", "project": project}
            if not reseeded and session.resumed and "No conversation found with session ID" in str(exc):
                host.close()
                mcp_config.unlink(missing_ok=True)
                self._chat_sessions.pop(project, None)
                return await self._chat_turn(project, text_value, claude_launch.SessionRef(uuid4()), True)
            if isinstance(exc, ClaudeTurnError):
                raise
            message = str(exc)
            reason = getattr(exc, "reason", None)
            if reason == "extra_usage_on":
                raise ClaudeTurnError("Claude extra usage is enabled", "claude_extra_usage_on") from exc
            if reason == "rate_limit_rejected":
                raise ClaudeTurnError(
                    "Claude allowance is exhausted", "claude_limit_reached", getattr(exc, "resets_at", None)
                ) from exc
            raise ClaudeTurnError(message, detail=reason) from exc
        except RpcError:
            raise
        except Exception as exc:
            if asyncio.current_task() in self._chat_stop_requests:
                return {"status": "stopped", "project": project}
            raise ClaudeTurnError(str(exc)) from exc
        finally:
            self._chat_sessions.pop(project, None)
            if self._chat_mcp_configs.get(project) == mcp_config:
                self._chat_mcp_configs.pop(project, None)
            host.close()
            mcp_config.unlink(missing_ok=True)

    async def _chat_send(self, project: str, text_value: str) -> dict[str, object]:
        if self._stopping:
            raise RpcError(-32001, "Engine is stopping", {"reason": "stopping"})
        old = self._chat_tasks.get(project)
        if old is not None and not old.done():
            raise RpcError(-32001, "Chat turn is already running", {"project": project})
        turn_id = str(uuid4())
        self._chat_turns[project] = turn_id
        task = asyncio.create_task(self._chat_turn(project, text_value))
        self._chat_tasks[project] = task
        try:
            return await task
        except ClaudeTurnError as exc:
            self._append_chat_entry(
                project,
                {
                    "turn": self._chat_turns.get(project, ""),
                    "role": "status",
                    "text": _TURN_FAILED_TEXT.get(exc.reason, "Turn failed."),
                },
            )
            data: dict[str, object] = {
                "project": project,
                "reason": exc.reason,
                "message": str(exc),
            }
            if exc.detail is not None:
                data["detail"] = exc.detail
            if exc.resets_at is not None:
                data["resetsAt"] = exc.resets_at
            raise RpcError(-32001, "Claude turn failed", data) from exc
        except asyncio.CancelledError:
            self._append_chat_entry(
                project, {"turn": self._chat_turns.get(project, ""), "role": "status", "text": "Turn stopped"}
            )
            if task in self._chat_stop_requests:
                return {"status": "stopped", "project": project}
            raise
        finally:
            if self._chat_tasks.get(project) is task:
                self._chat_tasks.pop(project, None)
            self._chat_stop_requests.discard(task)
            self._chat_turns.pop(project, None)

    async def _chat_stop(self, project: str) -> dict[str, object]:
        task = self._chat_tasks.get(project)
        session = self._chat_sessions.get(project)
        if task is not None and not task.done():
            self._chat_stop_requests.add(task)
        if session is not None:
            claude_launch.stop_turn(session)
        mcp_config = self._chat_mcp_configs.pop(project, None)
        if mcp_config is not None:
            mcp_config.unlink(missing_ok=True)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return {"status": "stopped", "project": project}

    async def _auth_status(self) -> dict[str, object]:
        profile = self._claude_profile()
        claude = await asyncio.to_thread(claude_auth_status, self._claude_binary(), profile)
        codex = await self._codex_allowance()
        windows = codex.get("windows") if codex else None
        first_window = windows[0] if isinstance(windows, list) and windows else None
        return {
            "claude": {"loggedIn": claude.ok},
            "codex": {
                "loggedIn": bool(codex and codex.get("loggedIn")),
                "planType": codex.get("planType") if codex else None,
                "usagePercent": first_window.get("usedPercent") if isinstance(first_window, dict) else None,
            },
        }

    @staticmethod
    def _epoch(value: object) -> int:
        try:
            from datetime import datetime

            return int(datetime.fromisoformat(str(value)).timestamp())
        except (TypeError, ValueError, OverflowError):
            return 0

    def _project_jobs(self, project: str) -> list[dict[str, object]]:
        rows = self.store.rows(
            "SELECT id,title,state,spec_version,created_at,updated_at FROM jobs "
            "WHERE project_id=? ORDER BY created_at DESC, rowid DESC",
            (project,),
        )
        return [
            {
                "job": str(r[0]),
                "title": str(r[1] or ""),
                "state": public_state(str(r[2])),
                "step": str(r[2]),
                "createdAt": self._epoch(r[4]),
                "updatedAt": self._epoch(r[5]),
                "specVersion": int(cast(str, r[3])),
            }
            for r in rows
        ]

    def _chat_history(self, project: str, params: dict[str, object]) -> dict[str, object]:
        self._project_dir(project)
        before = params.get("before")
        limit = params.get("limit", 100)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise RpcError(
                -32602, "Invalid params", {"field": "limit", "reason": "must be between 1 and 500"}
            )
        if before is not None and (not isinstance(before, int) or isinstance(before, bool)):
            raise RpcError(-32602, "Invalid params", {"field": "before", "reason": "must be an integer"})
        path = self._chat_history_path(project)
        entries: list[dict[str, object]] = []
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (
                    isinstance(value, dict)
                    and isinstance(value.get("id"), int)
                    and (before is None or value["id"] < before)
                ):
                    entries.append(value)
        entries.sort(key=lambda item: cast(int, item["id"]))
        cutoff = self.clock.now() - float(self._retention().chat_days * 86400)
        entries = [
            item
            for item in entries
            if not isinstance(item.get("ts"), (int, float)) or cast(float, item["ts"]) >= cutoff
        ]
        more = len(entries) > limit
        return {"entries": entries[-limit:], "more": more}

    def _codex_login_changed(self) -> bool:
        """True while a ChatGPT sign-in runs, and once after it ends: a cached read is stale then."""
        changed = False
        for process in tuple(self._codex_logins):
            changed = True
            if process.poll() is not None:
                self._codex_logins.discard(process)
        return changed

    def _codex_allowance_lock(self) -> asyncio.Lock:
        """One lock per event loop: concurrent status calls share one Codex start. Two Codex
        processes starting at once on one CODEX_HOME fail ("failed to initialize sqlite state")."""
        loop = asyncio.get_running_loop()
        lock = self._codex_allowance_locks.get(loop)
        if lock is None:
            lock = asyncio.Lock()
            # Only the current loop's lock is kept (tests run each call in its own loop).
            self._codex_allowance_locks = {loop: lock}
        return lock

    async def _codex_allowance(self) -> dict[str, object] | None:
        """The Codex allowance, None when signed out; raises when Codex cannot start or answer."""
        async with self._codex_allowance_lock():
            return await self._read_codex_allowance()

    async def _read_codex_allowance(self) -> dict[str, object] | None:
        now = self.clock.now()
        if self._codex_login_changed():
            self._codex_allowance_cache = None
        if self._codex_allowance_cache and now - self._codex_allowance_cache[0] < 300:
            return self._codex_allowance_cache[1]
        auth_dir = self.app_data / "auth-status"
        app_server = await self._launch(auth_dir)
        try:
            account = await app_server.client.request("account/read")
            if not account.get("account"):
                self._codex_allowance_cache = (now, None)
                return None
            try:
                limits = await app_server.client.request("account/rateLimits/read")
            except AppServerRpcError as exc:
                # "codex account authentication required to read rate limits" (or "chatgpt
                # authentication required"): signed out. -32600 alone is JSON-RPC's generic
                # invalid-request code, so any other error still surfaces.
                if exc.code != -32600 or "authentication required" not in exc.message.lower():
                    raise
                logging.getLogger("bca.api").warning("Codex is not signed in: %s", exc.message)
                self._codex_allowance_cache = (now, None)
                return None
            model = self.config.models.luna_model if self.config else ModelsConfig().luna_model
            snapshot = parse_rate_limits(limits, model)
            windows = (
                [
                    {
                        "name": "5h",
                        "usedPercent": snapshot.primary.used_percent,
                        "windowDurationMins": snapshot.primary.window_minutes,
                        "resetsAt": snapshot.primary.resets_at,
                    }
                ]
                if snapshot.primary
                else []
            )
            if snapshot.secondary:
                windows.append(
                    {
                        "name": "weekly",
                        "usedPercent": snapshot.secondary.used_percent,
                        "windowDurationMins": snapshot.secondary.window_minutes,
                        "resetsAt": snapshot.secondary.resets_at,
                    }
                )
            value = {
                "loggedIn": bool(account.get("account")),
                "planType": snapshot.plan_type,
                "windows": windows,
                "creditsBalance": str(snapshot.credits_balance)
                if snapshot.credits_balance is not None
                else None,
                "spendControlReached": snapshot.spend_control_reached,
                "stopAtPercent": (self.config.limits if self.config else LimitsConfig()).codex_stop_percent,
                "checkedAt": int(now),
            }
            safe_value = cast(dict[str, object], value)
            self._codex_allowance_cache = (now, safe_value)
            return safe_value
        finally:
            await app_server.stop()

    async def _codex_allowance_or_none(self) -> dict[str, object] | None:
        """The Codex allowance, or None when Codex cannot start or answer (shown as unknown)."""
        try:
            return await self._codex_allowance()
        except Exception:
            logging.getLogger("bca.api").warning("Codex allowance read failed", exc_info=True)
            return None

    async def _allowance_status(self) -> dict[str, object]:
        claude_value: dict[str, object] | None = None
        path = self.app_data / "claude" / "allowance.json"
        if path.is_file():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    claude_value = value
            except (OSError, json.JSONDecodeError):
                claude_value = None
        return {"codex": await self._codex_allowance_or_none(), "claude": claude_value}

    async def _start_codex_login(self) -> dict[str, object]:
        codex_home = (self.app_data / "codex").resolve()
        # Codex refuses to start when CODEX_HOME does not exist; the sign-in would fail silently.
        codex_home.mkdir(parents=True, exist_ok=True)
        env = childenv.build_child_env({"CODEX_HOME": str(codex_home)})
        kwargs: dict[str, Any] = {
            "env": env,
            "text": True,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if os.name == "nt":
            kwargs["creationflags"] = int(getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        else:
            kwargs["start_new_session"] = True
        # Sign in the same way the app-server reads it: credentials in the system keyring (no
        # auth.json in the app folder) and ChatGPT sign-in only.
        overrides = [
            item
            for key, value in LOCKDOWN_OVERRIDES
            if key in {"cli_auth_credentials_store", "forced_login_method"}
            for item in ("-c", f"{key}={value}")
        ]
        process = subprocess.Popen([str(self._codex_binary()), "login", *overrides], **kwargs)
        self._login_processes.add(process)
        self._codex_logins.add(process)
        # The cached read says "not signed in"; read again while and after the sign-in runs.
        self._codex_allowance_cache = None
        return {"started": True, "vendor": "codex"}

    def _job_status(self, job_id: str) -> dict[str, object]:
        row = self.store.rows(
            "SELECT project_id, state, spec_version, pause_reason, resume_at FROM jobs WHERE id=?", (job_id,)
        )
        if not row:
            return {"job": job_id, "state": "unknown"}
        progress = counts(self.store, job_id, sample=progress_sample_scope(self.store, job_id))
        state = str(row[0][1])
        return {
            "job": job_id,
            "project": str(row[0][0]),
            "state": public_state(state),
            "step": state,
            "specVersion": int(row[0][2]),
            "pauseReason": row[0][3],
            "resumeAt": row[0][4],
            "counts": {
                "done": progress.done,
                "inFlight": progress.in_flight,
                "remaining": progress.remaining,
                "failed": sum(progress.failed.values()),
                "heldBack": progress.held_back,
            },
            "stall": self._job_stall(job_id, state),
        }

    def _job_stall(self, job_id: str, state: str) -> dict[str, str] | None:
        if state != JobState.stalled.value:
            return None
        rows = self.store.rows(
            "SELECT data_json FROM job_events WHERE job_id=? AND kind='state' ORDER BY id DESC LIMIT 1",
            (job_id,),
        )
        reason = "job stalled"
        if rows:
            try:
                event = json.loads(str(rows[0][0]))
            except json.JSONDecodeError:
                event = {}
            if isinstance(event, dict) and isinstance(event.get("reason"), str) and event["reason"]:
                reason = event["reason"]
        return {"reason": reason, "text": _STALL_TEXT.get(reason, "The job stopped and needs attention.")}

    def _read_job_spec(self, job_id: str) -> tuple[Path, JobSpec, int, str]:
        row = self.store.rows("SELECT project_id FROM jobs WHERE id=?", (job_id,))
        if not row:
            raise RpcError(-32001, "Job not found", {"reason": "job_not_found", "job": job_id})
        project = str(row[0][0])
        folder = job_dir(self.app_data, project, job_id)
        try:
            spec, version = load_latest_spec(folder)
        except (OSError, ValueError) as exc:
            raise RpcError(-32001, "Job specification is unavailable", {"reason": "spec_not_found"}) from exc
        return folder, spec, version, project

    @staticmethod
    def _rule_value_text(value: object) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, float):
            return f"{value:g}"
        if isinstance(value, list):
            return ", ".join(ProductionBackend._rule_value_text(item) for item in value)
        if value is None:
            return "empty"
        return str(value)

    @staticmethod
    def _rule_conditions_text(match: str, conditions: tuple[Condition, ...]) -> str:
        parts = []
        for condition in conditions:
            words = _OPERATOR_WORDS.get(condition.op, condition.op)
            if condition.op in ("is_null", "not_null"):
                parts.append(f"{condition.field} {words}")
            else:
                parts.append(
                    f"{condition.field} {words} {ProductionBackend._rule_value_text(condition.value)}"
                )
        return (" or " if match == "any" else " and ").join(parts)

    @staticmethod
    def _rule_summary(rule: object) -> dict[str, object]:
        kind = str(getattr(rule, "kind", ""))
        definition = cast(Any, rule).to_dict()
        if isinstance(rule, InScopeRule):
            conditions = ProductionBackend._rule_conditions_text(rule.match, rule.conditions)
            return {
                "kind": kind,
                "summary": f"In scope if {conditions}",
                "definition": definition,
            }
        if isinstance(rule, ThresholdGroupRule):
            edges = [f"{edge:g}" for edge in rule.edges]
            # The evaluator puts an edge value in the lower band when inclusive_upper is set.
            below, above = ("<=", ">") if rule.inclusive_upper else ("<", ">=")
            ranges = [f"{below} {edges[0]}"] if edges else ["any value"]
            ranges += [f"{above} {edges[i]} and {below} {edges[i + 1]}" for i in range(len(edges) - 1)]
            if edges:
                ranges.append(f"{above} {edges[-1]}")
            bands = [
                f"{rule.labels[i]} ({text})" if i < len(rule.labels) else text
                for i, text in enumerate(ranges)
            ]
            return {
                "kind": kind,
                "name": rule.name,
                "summary": f"Bands for {rule.field}: {', '.join(bands)}",
                "definition": definition,
            }
        if isinstance(rule, ReviewFlagRule):
            conditions = ProductionBackend._rule_conditions_text(rule.match, rule.conditions)
            return {
                "kind": kind,
                "name": rule.name,
                "summary": f"Flag if {conditions}",
                "definition": definition,
            }
        return {"kind": kind, "summary": "Configured rule", "definition": definition}

    def _current_rules(self, folder: Path, spec: JobSpec, version: int) -> tuple[object, ...]:
        def rebuild_number(path: Path) -> int:
            match = re.search(r"\.rebuild(\d+)\.rules\.json$", path.name)
            return int(match.group(1)) if match else -1

        paths = sorted(
            (folder / "output").glob(f"results.v{version}.rebuild*.rules.json"),
            key=rebuild_number,
        )
        if paths:
            try:
                value = json.loads(paths[-1].read_text(encoding="utf-8"))
                if isinstance(value, list):
                    from bca.store.spec import _rule

                    return tuple(_rule(item) for item in value)
                raise ValueError("rules file must contain an array")
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise RpcError(
                    -32001, "Rebuild rules are unavailable", {"reason": "rules_unreadable"}
                ) from exc
        rows = self.store.rows(
            "SELECT data_json FROM job_events WHERE job_id=? AND kind='rules_rebuilt' "
            "ORDER BY id DESC LIMIT 1",
            (spec.job_id,),
        )
        if rows:
            try:
                value = json.loads(str(rows[0][0]))
                raw = value.get("rules") if isinstance(value, dict) else None
                if isinstance(raw, list):
                    from bca.store.spec import _rule

                    return tuple(_rule(item) for item in raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        return spec.rules

    @staticmethod
    def _preflight(folder: Path, version: int) -> dict[str, object]:
        path = folder / f"preflight.v{version}.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RpcError(-32001, "Preflight is unavailable", {"reason": "preflight_not_found"}) from exc
        if not isinstance(value, dict):
            raise RpcError(-32001, "Preflight is invalid", {"reason": "invalid_preflight"})
        return cast(dict[str, object], value)

    def _job_plan(self, job_id: str, params: dict[str, object]) -> dict[str, object]:
        folder, spec, version, _ = self._read_job_spec(job_id)
        requested = params.get("specVersion")
        if requested is not None and requested != version:
            raise RpcError(-32001, "Spec version is stale", {"reason": "stale_spec_version"})
        preflight = self._preflight(folder, version)
        buckets = cast(dict[str, object], preflight.get("buckets", {}))
        selected = sum(
            value if isinstance(value, int) else int(value)
            for value in buckets.values()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        )
        count_names = {
            "selected": "selected",
            "usable": "usable",
            "noTranscript": "no_transcript",
            "transcriptionFailed": "transcription_failed",
            "tooShort": "too_short",
            "notFound": "not_found",
        }
        counts_value = {
            public_name: int(cast(int | float, buckets.get(source_name, 0)))
            for public_name, source_name in count_names.items()
        }
        counts_value["selected"] = selected
        capped = preflight.get("capped_leads", 0)
        counts_value["capped"] = int(capped) if isinstance(capped, (int, float)) else 0
        intake = cast(dict[str, object], preflight.get("id_intake", {}))
        duplicates = intake.get("duplicates", 0)
        malformed = intake.get("malformed", 0)
        counts_value["duplicates"] = int(duplicates) if isinstance(duplicates, (int, float)) else 0
        counts_value["malformed"] = int(malformed) if isinstance(malformed, (int, float)) else 0
        properties = cast(dict[str, object], spec.output_schema.get("properties", {}))
        columns: list[dict[str, object]] = []
        for name, raw_value in properties.items():
            if not isinstance(raw_value, dict):
                continue
            value = cast(dict[str, object], raw_value)
            type_value = value.get("type", "string")
            public_type = (
                next((item for item in type_value if item != "null"), "string")
                if isinstance(type_value, list)
                else type_value
            )
            columns.append(
                {
                    "name": name,
                    "type": str(public_type),
                    "nullable": isinstance(type_value, list) and "null" in type_value,
                    "values": value.get("enum") if isinstance(value.get("enum"), list) else None,
                    **(
                        {"description": value["description"]}
                        if isinstance(value.get("description"), str)
                        else {}
                    ),
                }
            )
        source: dict[str, object] = {"kind": spec.lead_source.kind}
        if spec.lead_source.kind == "sql":
            source["sql"] = spec.lead_source.sql
        else:
            source["idCount"] = spec.lead_source.count
        dry_bytes = preflight.get("dry_run_bytes", 0)
        return {
            "job": job_id,
            "project": spec.project_id,
            "specVersion": version,
            "title": spec.title,
            "question": spec.question,
            "leadSource": source,
            "callsScope": {
                "mode": spec.calls_scope,
                "windowStart": spec.window_start,
                "windowEnd": spec.window_end,
            },
            "counts": counts_value,
            "scanBytes": int(dry_bytes) if isinstance(dry_bytes, (int, float)) else 0,
            "outputColumns": columns,
            "rules": [self._rule_summary(rule) for rule in self._current_rules(folder, spec, version)],
            "sampleSize": spec.sample_size,
            "estimate": None,
            "approved": bool(
                self.store.rows("SELECT plan_approved_at FROM jobs WHERE id=?", (job_id,))[0][0]
            ),
            "approvedAt": self.store.rows("SELECT plan_approved_at FROM jobs WHERE id=?", (job_id,))[0][0],
        }

    def _mask_sample_value(self, folder: Path, value: object) -> object:
        if isinstance(value, str):
            return mask_text(value, self._make_masker()(folder)).text
        if isinstance(value, dict):
            return {str(k): self._mask_sample_value(folder, v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._mask_sample_value(folder, item) for item in value]
        return value

    def _job_sample(self, job_id: str) -> dict[str, object]:
        folder, spec, version, _ = self._read_job_spec(job_id)
        rows = self.store.rows(
            "SELECT l.lead_id,l.status,r.answer_json,r.quotes_ok,r.flags_json "
            "FROM leads l LEFT JOIN results r ON r.job_id=l.job_id AND r.lead_id=l.lead_id "
            "WHERE l.job_id=? AND l.in_sample=1 ORDER BY l.lead_id",
            (job_id,),
        )
        result_rows: list[dict[str, object]] = []
        for row in rows:
            answer = json.loads(str(row[2])) if row[2] is not None else {}
            flags = json.loads(str(row[4])) if row[4] is not None else []
            if not isinstance(answer, dict):
                answer = {}
            if not isinstance(flags, list):
                flags = []
            masked = cast(dict[str, object], self._mask_sample_value(folder, answer))
            quotes = [
                {
                    "field": field.field,
                    "text": masked.get(field.field, ""),
                    "ok": bool(row[3])
                    and not any(str(flag).startswith(f"quote:{field.field}:") for flag in flags),
                }
                for field in spec.quote_fields
            ]
            result_rows.append(
                {
                    "lead": str(row[0]),
                    "answer": masked,
                    "quotes": quotes,
                    "flags": flags,
                    "status": "repairing"
                    if str(row[1]) == "queued" and any("quote:" in str(flag) for flag in flags)
                    else str(row[1]),
                }
            )
        read = sum(item["status"] in {"done", "repairing"} for item in result_rows)
        return {
            "job": job_id,
            "specVersion": version,
            "total": len(result_rows),
            "read": read,
            "quotesVerified": sum(
                all(bool(q["ok"]) for q in cast(list[dict[str, object]], item["quotes"]))
                for item in result_rows
            ),
            "quotesRepaired": 0,
            "quotesFlagged": sum(bool(item["flags"]) for item in result_rows),
            "heldBack": sum(item["status"] == "held_back" for item in result_rows),
            "failed": sum(item["status"] == "failed" for item in result_rows),
            "rows": result_rows,
            "columns": list(cast(dict[str, object], spec.output_schema.get("properties", {}))),
            "file": (folder / "output" / f"sample.v{version}.xlsx")
            .relative_to(folder.parent.parent)
            .as_posix()
            if (folder / "output" / f"sample.v{version}.xlsx").is_file()
            else None,
        }

    def _job_snapshot(self, job_id: str) -> dict[str, object]:
        folder, spec, version, project = self._read_job_spec(job_id)
        row = self.store.rows(
            "SELECT state,pause_reason,resume_at,resume_state,plan_approved_at,sample_approved_at "
            "FROM jobs WHERE id=?",
            (job_id,),
        )[0]
        sample_scope = progress_sample_scope(self.store, job_id)
        progress = counts(self.store, job_id, sample=sample_scope)
        sample_counts = counts(self.store, job_id, sample=True)
        whole = counts(self.store, job_id, sample=None)
        state = str(row[0])
        step_names = (
            "plan approved",
            "leads selected",
            "transcripts pulled",
            "sample masked",
            "sample read",
            "sample approved",
            "masking",
            "full run",
            "quote check and repair",
            "Excel built",
            "verified",
            "summary written",
        )
        step_state = {
            "plan_proposed": 0,
            "plan_approved": 0,
            "pulling": 2,
            "masking_sample": 3,
            "sample_running": 4,
            "sample_ready": 5,
            "sample_approved": 6,
            "full_running": 7,
            "quote_check": 8,
            "building_output": 9,
            "verifying": 10,
            "done": 12,
            "done_with_issues": 12,
        }
        effective_state = state
        if state in {JobState.paused.value, JobState.stalled.value} and row[3]:
            effective_state = str(row[3])
        elif state in {JobState.failed.value, JobState.cancelled.value}:
            for event_row in self.store.rows(
                "SELECT data_json FROM job_events WHERE job_id=? AND kind='state' ORDER BY id DESC",
                (job_id,),
            ):
                try:
                    event = json.loads(str(event_row[0]))
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict) and str(event.get("to")) not in {
                    JobState.failed.value,
                    JobState.cancelled.value,
                }:
                    effective_state = str(event.get("to", effective_state))
                    break
        active = step_state.get(effective_state, -1)
        state_events: dict[str, int] = {}
        for event_row in self.store.rows(
            "SELECT data_json,ts FROM job_events WHERE job_id=? AND kind='state'", (job_id,)
        ):
            try:
                event_data = json.loads(str(event_row[0]))
            except json.JSONDecodeError:
                continue
            if isinstance(event_data, dict) and isinstance(event_data.get("to"), str):
                state_events[event_data["to"]] = self._epoch(event_row[1])
        plan_approved_at = self._epoch(row[4]) if row[4] is not None else None
        sample_approved_at = self._epoch(row[5]) if row[5] is not None else None
        # A passed gate shows when the operator approved it, not when it started waiting.
        gate_approved_at = {0: plan_approved_at, 5: sample_approved_at}
        steps = []
        for index, name in enumerate(step_names):
            status = "done" if index < active else "active" if index == active else "todo"
            if index in {0, 5} and status == "active":
                status = "waiting_for_you"
            step_ts = next((state_events.get(key) for key in step_state if step_state[key] == index), None)
            approved_ts = gate_approved_at.get(index)
            if status == "done" and approved_ts is not None:
                step_ts = approved_ts
            steps.append(
                {
                    "id": name.replace(" ", "_"),
                    "label": name,
                    "status": status,
                    "ts": step_ts,
                    "detail": (
                        f"{sample_counts.done}/{sample_counts.total}"
                        if index == 4 and status in {"active", "done"}
                        else f"{whole.done}/{whole.total}"
                        if index == 7 and status in {"active", "done"}
                        else None
                    ),
                }
            )
        activity = [
            {"ts": self._epoch(r[1]), "kind": str(r[2]), "text": str(r[3])}
            for r in self.store.rows(
                "SELECT ts,ts,kind,text FROM job_activity WHERE job_id=? ORDER BY id DESC LIMIT 20", (job_id,)
            )
        ]
        rec_path = folder / "output" / f"reconciliation.v{version}.json"
        reconciliation = json.loads(rec_path.read_text(encoding="utf-8")) if rec_path.is_file() else None
        eta: dict[str, object] | None = None
        activity_value: dict[str, object] = {
            "lastActivityTs": activity[0]["ts"] if activity else None,
            "inFlight": progress.in_flight,
            "workers": self.concurrency,
            "throughputPerMin": None,
        }
        quota: dict[str, object] = {"codex": None, "claude": None, "projectedEndPercent": None}
        runner = self.jobs.runners.get(job_id)
        task = self.jobs.tasks.get(job_id)
        if runner is not None and task is not None and not task.done():
            now = self.clock.now()
            live = counts(self.store, job_id, sample=sample_scope)
            estimate = runner._eta.estimate(now, live.remaining, self.concurrency)
            quota_rows = self.store.rows(
                "SELECT data_json FROM job_events WHERE job_id=? AND kind='quota' ORDER BY id", (job_id,)
            )
            quota_history: list[SimpleNamespace] = []
            last_quota: dict[str, object] = {}
            for quota_row in quota_rows:
                try:
                    item = json.loads(str(quota_row[0]))
                    if isinstance(item, dict):
                        last_quota = item
                    primary = item.get("primary") if isinstance(item, dict) else None
                    if isinstance(item, dict) and isinstance(primary, dict):
                        quota_history.append(
                            SimpleNamespace(
                                used_percent=int(primary.get("usedPercent", 0)),
                                resets_at=primary.get("resetsAt"),
                                taken_at=float(item.get("takenAt", now)),
                            )
                        )
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
            if quota_history:
                latest = quota_history[-1]
                quota_guard = runner._quota_guard
                # The same percent-per-completed-lead rate the ETA uses (None until leads finish
                # while usage rises): the job ends near used + leads still to read x that rate.
                rate = runner._eta.percent_per_lead(quota_history)
                if rate is not None:
                    leads_left = live.remaining + live.retrying + live.in_flight
                    quota["projectedEndPercent"] = min(100, round(latest.used_percent + leads_left * rate))
                estimate = runner._eta.with_quota(
                    estimate, quota_history, live.remaining, quota_guard.stop_percent, now
                )
                quota["codex"] = self._quota_public(last_quota)
                cast(dict[str, object], quota["codex"])["stopAtPercent"] = quota_guard.stop_percent
            eta = estimate.public()
            activity_value.update(
                {
                    "lastActivityTs": runner._last_activity_ts,
                    "inFlight": live.in_flight,
                    "workers": getattr(runner._phase_pool, "concurrency", self.concurrency),
                    "throughputPerMin": self._throughput(runner, now),
                }
            )
        return {
            "job": job_id,
            "project": project,
            "title": spec.title,
            "state": public_state(state),
            "step": state,
            "specVersion": version,
            "pauseReason": row[1],
            "resumeAt": row[2],
            "planApprovedAt": plan_approved_at,
            "sampleApprovedAt": sample_approved_at,
            # The app's start card reads approvedAt: the time the sample was approved.
            **({"approvedAt": sample_approved_at} if sample_approved_at is not None else {}),
            "progress": {
                "job": job_id,
                "project": project,
                "phase": "sample" if sample_scope is True else "full",
                "done": progress.done,
                "inFlight": progress.in_flight,
                "retrying": progress.retrying,
                "failed": sum(progress.failed.values()),
                "heldBack": progress.held_back,
                "remaining": progress.remaining,
                "total": progress.total,
            },
            "counts": {
                "selected": whole.total
                + whole.no_transcript
                + whole.transcription_failed
                + whole.too_short
                + whole.not_found,
                "usable": whole.total,
                "noTranscript": whole.no_transcript,
                "transcriptionFailed": whole.transcription_failed,
                "tooShort": whole.too_short,
                "notFound": whole.not_found,
                "capped": whole.capped,
                "failedByReason": whole.failed,
            },
            "eta": eta,
            "activity": activity_value,
            "quota": quota,
            "steps": steps,
            "recent": list(reversed(activity)),
            "reconciliation": reconciliation,
            "failure": self._job_failure(job_id, state),
            "stall": self._job_stall(job_id, state),
        }

    def _job_failure(self, job_id: str, state: str) -> dict[str, str] | None:
        if state not in {JobState.failed.value, JobState.cancelled.value}:
            return None
        reason = state
        text_reason = state
        rows = self.store.rows(
            "SELECT data_json FROM job_events WHERE job_id=? AND kind='state' ORDER BY id DESC LIMIT 1",
            (job_id,),
        )
        if rows:
            try:
                event = json.loads(str(rows[0][0]))
            except json.JSONDecodeError:
                event = {}
            if isinstance(event, dict) and isinstance(event.get("reason"), str) and event["reason"]:
                text_reason = str(event["reason"])
        return {
            "reason": reason,
            "text": _FAILURE_TEXT.get(
                text_reason, _FAILURE_TEXT.get(reason, "The job ended because of an unspecified failure.")
            ),
        }

    def _job_outputs(self, job_id: str) -> dict[str, object]:
        folder, spec, version, _ = self._read_job_spec(job_id)
        row = self.store.rows(
            "SELECT state,finished_at,created_at,full_started_at FROM jobs WHERE id=?", (job_id,)
        )[0]
        project_root = folder.parent.parent
        files = [
            {
                "name": p.name,
                "path": p.relative_to(project_root).as_posix(),
                "kind": self._file_kind(p.name),
                "specVersion": int(match.group(1))
                if (match := re.search(r"\.v(\d+)(?:\.rebuild(\d+))?\.", p.name))
                else None,
                "rebuild": int(match.group(2)) if match and match.group(2) else None,
            }
            # Sorted by name: glob order differs between file systems (it did on macOS).
            for p in sorted((folder / "output").glob("*"), key=lambda item: item.name)
            if self._finished_output(p)
        ]
        summary = folder.parent.parent / "summary.md"
        if summary.is_file():
            files.append(
                {
                    "name": "summary.md",
                    "path": "summary.md",
                    "kind": "markdown",
                    "specVersion": None,
                    "rebuild": None,
                }
            )
        path = folder / "output" / f"reconciliation.v{version}.json"
        reconciliation = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
        if isinstance(reconciliation, dict):
            reconciliation = self._public_reconciliation(reconciliation)
        rec = reconcile(self.store, job_id, final=False)
        issues: list[dict[str, object]] = []
        for kind, statuses, text in (
            ("held_back", ("held_back",), "Leads held back by masking or data quality"),
            ("failed", ("failed",), "Leads failed after retries"),
        ):
            lead_rows = self.store.rows(
                "SELECT lead_id FROM leads WHERE job_id=? AND status IN (?) ORDER BY lead_id",
                (job_id, statuses[0]),
            )
            if lead_rows:
                issues.append(
                    {
                        "kind": kind,
                        "count": len(lead_rows),
                        "text": text,
                        "leads": [str(item[0]) for item in lead_rows[:50]],
                    }
                )
        quote_rows = self.store.rows(
            "SELECT lead_id FROM results WHERE job_id=? AND flags_json LIKE '%quote:%' ORDER BY lead_id",
            (job_id,),
        )
        if quote_rows:
            issues.append(
                {
                    "kind": "quote_flag",
                    "count": len(quote_rows),
                    "text": "Quotes require review",
                    "leads": [str(item[0]) for item in quote_rows[:50]],
                }
            )
        capped_rows = self.store.rows(
            "SELECT lead_id FROM leads WHERE job_id=? AND calls_dropped_cap>0 ORDER BY lead_id", (job_id,)
        )
        if capped_rows:
            issues.append(
                {
                    "kind": "capped",
                    "count": len(capped_rows),
                    "text": "Leads had calls capped",
                    "leads": [str(item[0]) for item in capped_rows[:50]],
                }
            )
        finished = self._epoch(row[1]) if row[1] else None
        started = self._epoch(row[3]) if row[3] else self._epoch(row[2])
        failure = self._job_failure(job_id, str(row[0]))
        return {
            "job": job_id,
            "specVersion": spec.spec_version,
            "state": public_state(str(row[0])),
            "finishedAt": finished,
            "durationSeconds": max(0, finished - started) if finished is not None and started else None,
            "asleepSeconds": None,
            "files": files,
            "reconciliation": reconciliation or {"selected": rec.distinct_ids_in, "addsUp": rec.ok},
            "issues": issues,
            "headline": None,
            "failure": failure,
        }

    @staticmethod
    def _finished_output(path: Path) -> bool:
        """A workbook or note the operator can open: not a temp file or an empty reserved name."""
        if path.name.startswith(".") or path.suffix.lower() not in {".xlsx", ".md"}:
            return False
        try:
            return path.is_file() and path.stat().st_size > 0
        except OSError:
            return False

    @staticmethod
    def _public_reconciliation(value: dict[str, object]) -> dict[str, object]:
        raw_terms = value.get("terms")
        terms = raw_terms if isinstance(raw_terms, dict) else {}
        return {
            "selected": value.get("selected_distinct_ids", 0),
            "read": terms.get("done", 0),
            "noTranscript": terms.get("no_transcript", 0),
            "transcriptionFailed": terms.get("transcription_failed", 0),
            "tooShort": terms.get("too_short", 0),
            "notFound": terms.get("not_found", 0),
            "heldBack": terms.get("held_back", 0),
            "failed": terms.get("failed", 0),
            "notStarted": terms.get("not_started", 0),
            "addsUp": value.get("adds_up", False),
        }

    @staticmethod
    def _quota_public(value: dict[str, object]) -> dict[str, object]:
        return {
            key: value.get(key)
            for key in (
                "limitId",
                "primary",
                "secondary",
                "creditsBalance",
                "hasCredits",
                "spendControlReached",
                "rateLimitReachedType",
                "ordinaryUsageAllowed",
                "planType",
                "stopAtPercent",
            )
        }

    @staticmethod
    def _throughput(runner: JobRunner, now: float) -> float | None:
        points = [point for point in runner._eta._completed if now - point <= 1800]
        if len(points) < 2:
            return None
        active = runner._eta._excluded(points[0], points[-1])
        elapsed = points[-1] - points[0] - active
        return (len(points) - 1) / elapsed * 60 if elapsed > 0 else None

    def _job_state(self, job_id: str) -> JobState:
        row = self.store.rows("SELECT state FROM jobs WHERE id=?", (job_id,))
        if not row:
            raise RpcError(-32001, "Job not found", {"job": job_id})
        return JobState(str(row[0][0]))

    def _publish_transition(self, job_id: str, before: str, after: str, reason: str | None) -> None:
        row = self.store.rows("SELECT project_id FROM jobs WHERE id=?", (job_id,))
        self.event_bus.publish(
            Event(
                "state",
                job_id,
                self.clock.now(),
                {
                    "from": before,
                    "to": after,
                    "reason": reason,
                    "source": "store",
                    "project": str(row[0][0]) if row else "",
                },
            )
        )

    def _validate_job_operation(self, job_id: str, method: str) -> None:
        state = self._job_state(job_id)
        if state is JobState.stalled:
            rows = self.store.rows(
                "SELECT data_json FROM job_events WHERE job_id=? AND kind='state' ORDER BY id DESC LIMIT 1",
                (job_id,),
            )
            stall_reason = "job stalled"
            if rows:
                try:
                    data = json.loads(str(rows[0][0]))
                except json.JSONDecodeError:
                    data = {}
                if isinstance(data, dict) and isinstance(data.get("reason"), str):
                    stall_reason = data["reason"]
            raise RpcError(-32001, "Job is stalled", {"reason": "job_stalled", "stallReason": stall_reason})
        required = {
            "job/resume": (JobState.paused, "job_not_paused"),
            "job/pause": (
                {
                    JobState.pulling,
                    JobState.masking_sample,
                    JobState.sample_running,
                    JobState.full_running,
                    JobState.quote_check,
                    JobState.building_output,
                    JobState.verifying,
                },
                "job_not_running",
            ),
            "job/startFull": (JobState.sample_approved, "sample_not_approved"),
        }
        expected, reason = required[method]
        valid = state in expected if isinstance(expected, set) else state is expected
        if not valid:
            raise RpcError(-32001, "Invalid job state", {"reason": reason})

    def _settings(self) -> dict[str, object]:
        cfg = self.config
        key = cfg.bigquery.key_path
        saved = self._saved_settings()
        saved_worker = saved.get("workerCount", self.concurrency)
        return {
            "connectorPath": str(key) if key else None,
            "keyPath": str(key) if key else None,
            "connectorDir": str(cfg.bigquery.connector_dir) if cfg.bigquery.connector_dir else None,
            "keyLocated": bool(key and key.is_file()),
            "workerCount": 4 if saved_worker is None else saved_worker,
            "workerCountMode": "auto" if saved_worker is None else "fixed",
            "retentionDays": self._retention().masked_cache_days,
            "claudeModel": cfg.models.claude_model,
            "codexModel": cfg.models.luna_model,
        }

    @staticmethod
    def _is_internal_project_file(relative: Path) -> bool:
        # Windows file names ignore case, so CHAT/History.jsonl is the chat history too.
        parts = tuple(part.lower() for part in relative.parts)
        name = parts[-1] if parts else ""
        if "chat" in parts or name == "harvested_names.json":
            return True
        if len(parts) >= 3 and parts[0] == "jobs" and parts[2] in {"raw", "masked"}:
            return True
        if name.startswith(".bca-") or name.endswith((".db", ".sqlite", ".sqlite3")):
            return True
        return name.endswith(".log") and ("codex" in name or "app-server" in name)

    def _write_settings(self, changes: dict[tuple[str, str], str]) -> None:
        if not self.config_path.is_file():
            raise RpcError(-32001, "site config is not available")
        lines = self.config_path.read_text(encoding="utf-8-sig").splitlines()
        for (section, key), value in changes.items():
            header = f"[{section}]"
            start = next((i for i, line in enumerate(lines) if line.strip() == header), None)
            if start is None:
                lines.extend(["", header, f"{key} = {value}"])
            else:
                end = next(
                    (i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines)
                )
                for i in range(start + 1, end):
                    if lines[i].lstrip().startswith(key + " ") or lines[i].lstrip().startswith(key + "="):
                        lines[i] = f"{key} = {value}"
                        break
                else:
                    lines.insert(end, f"{key} = {value}")
        content = "\n".join(lines) + "\n"
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=self.config_path.parent, delete=False
        ) as temporary:
            temporary.write(content)
            candidate = Path(temporary.name)
        try:
            load_config(candidate)
            os.replace(candidate, self.config_path)
        finally:
            candidate.unlink(missing_ok=True)

    @staticmethod
    def _validate_settings(params: dict[str, object]) -> None:
        allowed = {
            "connectorPath",
            "keyPath",
            "workerCount",
            "retentionDays",
            "claudeModel",
            "codexModel",
            "confirmModelChange",
        }
        unknown = sorted(set(params) - allowed)
        if unknown:
            raise RpcError(-32602, "Invalid params", {"fields": unknown, "reason": "unknown field"})
        for key in ("connectorPath", "keyPath", "claudeModel", "codexModel"):
            if key in params and (not isinstance(params[key], str) or not params[key]):
                raise RpcError(-32602, "Invalid params", {"field": key, "reason": "must be non-empty"})
        claude = params.get("claudeModel")
        if claude is not None and (
            not isinstance(claude, str)
            # Letters, digits, dots and dashes only: it goes on Claude's command line.
            or not re.fullmatch(r"claude-[a-z0-9][a-z0-9.-]*", claude)
            or claude in {"claude-", "claude-opus", "claude-x"}
            or "[" in claude
            or claude.endswith("plan")
            or claude.endswith("-latest")
        ):
            raise RpcError(-32602, "Invalid params", {"field": "claudeModel", "reason": "invalid model id"})
        luna = params.get("codexModel")
        if luna is not None and (
            not isinstance(luna, str) or not re.fullmatch(r"gpt-[0-9]+(?:\.[0-9]+)*-luna", luna)
        ):
            raise RpcError(-32602, "Invalid params", {"field": "codexModel", "reason": "invalid model id"})
        for key in ("workerCount", "retentionDays"):
            value = params.get(key)
            if (
                key in params
                and value is not None
                and (
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or value < 1
                    or (key == "retentionDays" and value > 3650)
                )
            ):
                raise RpcError(-32602, "Invalid params", {"field": key, "reason": "must be positive"})

    async def call(self, method: str, params: dict[str, object]) -> object:
        try:
            self._reload_config()
            if self.config is not None:
                self._start_resume_watcher()
                self.jobs.schedule_recovery()
            if self._stopping and method != "shutdown":
                raise RpcError(-32001, "Engine is stopping", {"reason": "stopping"})
            if method == "app/status":
                running = [
                    str(r[0])
                    for r in self.store.rows(
                        "SELECT id FROM jobs WHERE state IN ("
                        "'pulling','masking_sample','sample_running','full_running',"
                        "'quote_check','building_output','verifying')"
                    )
                ]
                return {
                    "state": "ready",
                    "protocol": "2.0",
                    "engineVersion": "0.1.0",
                    "dataDir": str(self.app_data),
                    "runningJobs": running,
                }
            if method == "shutdown":
                self._stopping = True
                await self.jobs.shutdown()
                if self._resume_watcher_task is not None:
                    self._resume_watcher_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await self._resume_watcher_task
                self.store.close()
                self._closed = True
                return {"stopping": True, "exitCode": 0}
            if method == "setup/checklist":
                items = Checklist(self.app_data).items
                if self.config is None:
                    for item in items:
                        if item.name == "connector":
                            item.status = "failed"
                            item.detail = f"BigQuery settings not found; put site.toml in {self.app_data}."
                            break
                return [item.__dict__ for item in items]
            if method == "setup/runChecks":
                checklist = Checklist(self.app_data)
                try:
                    claude_probe = await asyncio.to_thread(
                        claude_auth_status, self._claude_binary(), self._claude_profile()
                    )
                except OSError:
                    claude_probe = None
                # Any failure of the Codex read is a failed sign-in row, not a failed checklist.
                codex_probe = await self._codex_allowance_or_none()
                # The plain-words probes, plus the two sign-in rows read above.
                probes = {
                    **self._setup_probes(),
                    "claude_login": lambda: (
                        bool(claude_probe and claude_probe.ok),
                        "Signed in" if claude_probe and claude_probe.ok else "Not signed in yet",
                    ),
                    "chatgpt_login": lambda: (
                        bool(codex_probe and codex_probe.get("loggedIn")),
                        "Signed in" if codex_probe and codex_probe.get("loggedIn") else "Not signed in yet",
                    ),
                }
                return {"items": [item.__dict__ for item in checklist.run_checks(probes)]}
            if method == "setup/markManual":
                item = cast(str, params["item"])
                if item not in MANUAL:
                    raise RpcError(-32602, "Invalid params", {"field": "item", "reason": "not a manual item"})
                checklist = Checklist(self.app_data)
                checklist.mark_manual(item)
                return {"item": item, "status": "manual_done"}
            if method == "settings/get":
                self._require_config()
                return self._settings()
            if method == "settings/set":
                self._require_config()
                if (
                    any(key in params for key in ("claudeModel", "codexModel"))
                    and params.get("confirmModelChange") is not True
                ):
                    raise RpcError(
                        -32602,
                        "Invalid params",
                        {
                            "field": "confirmModelChange",
                            "reason": "confirmModelChange required to change model ids",
                        },
                    )
                self._validate_settings(params)
                saved = self._saved_settings()
                if "workerCount" in params:
                    saved["workerCount"] = params["workerCount"]
                if "retentionDays" in params:
                    saved["retentionDays"] = params["retentionDays"]
                if "workerCount" in params or "retentionDays" in params:
                    self._save_settings(saved)
                changes: dict[tuple[str, str], str] = {}
                if "claudeModel" in params:
                    changes[("models", "claude_model")] = json.dumps(params["claudeModel"])
                if "codexModel" in params:
                    changes[("models", "luna_model")] = json.dumps(params["codexModel"])
                key_path = params.get("keyPath", params.get("connectorPath"))
                if key_path is not None:
                    changes[("bigquery", "key_path")] = json.dumps(key_path)
                if changes:
                    self._write_settings(changes)
                try:
                    self.config = load_config(self.config_path)
                except ConfigError as exc:
                    field_by_key = {
                        "claudeModel": "models.claude_model",
                        "codexModel": "models.luna_model",
                        "connectorPath": "bigquery.key_path",
                        "retentionDays": "retention.masked_cache_days",
                    }
                    field = next(
                        (field_by_key[key] for key in field_by_key if key in params),
                        "configuration",
                    )
                    raise RpcError(
                        -32602,
                        "Invalid configuration",
                        {"field": field, "reason": str(exc).split(": ", 1)[-1]},
                    ) from exc
                if "workerCount" in params and params["workerCount"] is not None:
                    self.concurrency = cast(int, params["workerCount"])
                elif params.get("workerCount", object()) is None:
                    self.concurrency = 4
                self.config_error = None
                self.jobs.recover()
                self.jobs.schedule_recovery()
                return self._settings()
            if method == "projects/list":
                self._require_config()
                result = []
                for r in self.store.rows("SELECT id,title,created_at FROM projects ORDER BY created_at DESC"):
                    jobs = self._project_jobs(str(r[0]))
                    result.append(
                        {
                            "project": str(r[0]),
                            "title": str(r[1]),
                            "createdAt": self._epoch(r[2]),
                            "activeJob": next(
                                (
                                    j
                                    for j in jobs
                                    if j["state"] not in {"done", "done_with_issues", "failed", "cancelled"}
                                ),
                                None,
                            ),
                            "lastJob": jobs[0] if jobs else None,
                        }
                    )
                return result
            if method == "projects/jobs":
                self._require_config()
                project = _slug(params.get("project"), "project")
                self._project_dir(project)
                return {"jobs": self._project_jobs(project)}
            if method == "projects/create":
                self._require_config()
                project = _slug(params.get("project"), "project")
                title = params.get("title")
                if not isinstance(title, str) or not title.strip():
                    raise RpcError(
                        -32602, "Invalid params", {"field": "title", "reason": "must not be empty"}
                    )
                self.store.create_project(project, title)
                (self.app_data / "projects" / project).mkdir(parents=True, exist_ok=False)
                return {"project": project, "title": title}
            if method == "files/list":
                self._require_config()
                value = params.get("project")
                if not isinstance(value, str) or _SLUG.fullmatch(value) is None:
                    raise RpcError(
                        -32602, "Invalid params", {"field": "project", "reason": "path outside project"}
                    )
                project = value
                root = (self.app_data / "projects" / project).resolve()
                if not root.is_dir():
                    raise RpcError(-32001, "Project not found", {"project": project})
                files = [
                    {
                        "name": p.relative_to(root).as_posix(),
                        "size": p.stat().st_size,
                        "modified": float(p.stat().st_mtime),
                        "kind": self._file_kind(p.name),
                    }
                    for p in root.rglob("*")
                    if p.is_file() and not self._is_internal_project_file(p.relative_to(root))
                ]
                return sorted(files, key=lambda item: cast(float, item["modified"]), reverse=True)
            if method == "files/path":
                project = _slug(params.get("project"), "project")
                name = params.get("name")
                if (
                    not isinstance(name, str)
                    or not name
                    or Path(name).is_absolute()
                    or ".." in Path(name).parts
                    or any(part.startswith(".") for part in Path(name).parts)
                ):
                    raise RpcError(-32602, "Invalid params", {"field": "name", "reason": "invalid_name"})
                root = self._project_dir(project)
                path = (root / Path(*name.split("/"))).resolve()
                if not path.is_file() or self._is_internal_project_file(path.relative_to(root)):
                    raise RpcError(-32001, "File not found", {"reason": "file_not_found"})
                return {"path": str(path), "projectDir": str(root)}
            if method == "ids/attach":
                project = _slug(params.get("project"), "project")
                raw = params.get("text")
                if not isinstance(raw, str) or len(raw.encode("utf-8")) > 5 * 1024 * 1024:
                    raise RpcError(-32602, "Invalid params", {"reason": "too_large"})
                intake = parse_id_list(raw)
                if len(intake.ids) > 200_000:
                    raise RpcError(-32602, "Invalid params", {"reason": "too_large"})
                if not intake.ids:
                    raise RpcError(-32602, "Invalid params", {"reason": "no_ids"})
                root = self._project_dir(project) / "inputs"
                root.mkdir(parents=True, exist_ok=True)
                n = (
                    max(
                        [
                            int(p.stem.removeprefix("ids-"))
                            for p in root.glob("ids-*.txt")
                            if p.stem.removeprefix("ids-").isdigit()
                        ]
                        or [0]
                    )
                    + 1
                )
                target = root / f"ids-{n}.txt"
                temp = target.with_name(f".{target.name}.tmp")
                temp.write_text("\n".join(intake.ids) + "\n", encoding="utf-8", newline="\n")
                temp.replace(target)
                return {
                    "name": target.name,
                    "raw": intake.raw_count,
                    "distinct": len(intake.ids),
                    "duplicates": intake.duplicates,
                    "malformed": len(intake.malformed),
                }
            if method == "chat/history":
                return self._chat_history(cast(str, params["project"]), params)
            if method == "allowance/status":
                return await self._allowance_status()
            if method.startswith("chat/") or method.startswith("auth/"):
                if method.startswith("chat/"):
                    self._require_config()
                if method == "chat/send":
                    return await self._chat_send(cast(str, params["project"]), cast(str, params["text"]))
                if method == "chat/stop":
                    return await self._chat_stop(cast(str, params["project"]))
                if method == "auth/claudeLogin":
                    self._login_processes.add(
                        start_claude_login(self._claude_binary(), self._claude_profile())
                    )
                    return {"started": True, "vendor": "claude"}
                if method == "auth/codexLogin":
                    return await self._start_codex_login()
                if method == "auth/status":
                    return await self._auth_status()
                raise RpcError(-32601, "Method not found")
            if method == "os/powerStatus":
                self._require_config()
                was_blocked = not self._can_dispatch()
                self.power_status = {
                    "onAc": cast(bool | None, params.get("onAc")),
                    "percent": cast(int | None, params.get("percent")),
                }
                if self._can_dispatch():
                    for runner in self.jobs.runners.values():
                        runner.signal_dispatch()
                if was_blocked and self._can_dispatch():

                    async def resume_after_power(runner: JobRunner) -> None:
                        await runner.resume("automatic")

                    for job_id in self._running_job_ids(include_paused=True):
                        row = self.store.rows("SELECT pause_reason FROM jobs WHERE id=?", (job_id,))
                        if row and str(row[0][0]) == PauseReason.power.value:
                            await self.jobs.start(job_id, resume_after_power)
                return {"status": "accepted"}
            if method == "os/suspendHint":
                self._require_config()
                self.sleep.os_suspend_hint(self.clock.now())
                for job_id in self._running_job_ids():
                    with contextlib.suppress(Exception):
                        runner = self.jobs.runner(job_id)
                        runner.request_stop_for_prepare()
                        await runner._pause_with_pool(PauseReason.sleep.value, None, wait_for_phase=False)
                        await self.jobs.stop(job_id, timeout=1.0)
                return {"status": "accepted"}
            if method == "os/resumeHint":
                self._require_config()
                events = self.sleep.os_resume_hint(self.clock.now())
                for event in events:
                    self.event_bus.publish(event)

                async def resume_after_sleep(runner: JobRunner) -> None:
                    await runner.resume("automatic")

                for job_id in self._running_job_ids(include_paused=True):
                    row = self.store.rows("SELECT state, pause_reason FROM jobs WHERE id=?", (job_id,))
                    if (
                        row
                        and str(row[0][0]) == JobState.paused.value
                        and str(row[0][1]) == PauseReason.sleep.value
                    ):
                        await self.jobs.start(job_id, resume_after_sleep)
                return {
                    "status": "accepted",
                    "actions": [action.name for action in self.sleep.wake_actions()],
                }
            if method == "job/status":
                self._require_config()
                return self._job_status(cast(str, params["job"]))
            if method == "job/plan":
                self._require_config()
                return self._job_plan(cast(str, params["job"]), params)
            if method == "job/sample":
                self._require_config()
                return self._job_sample(cast(str, params["job"]))
            if method == "job/snapshot":
                self._require_config()
                return self._job_snapshot(cast(str, params["job"]))
            if method == "job/outputs":
                self._require_config()
                return self._job_outputs(cast(str, params["job"]))
            job = cast(str, params.get("job", ""))
            self._require_config()
            if method == "job/approvePlan":
                expected = cast(int, params["expectedSpecVersion"])
                old = self.jobs.tasks.get(job)
                if old is not None and not old.done():
                    raise RpcError(-32001, "Job is already running", {"job": job, "reason": "job_running"})
                try:
                    approve_plan(self.store, job, "operator", expected)
                except TransitionError as exc:
                    raise RpcError(-32001, "Plan approval failed", {"reason": "invalid_job_state"}) from exc
                except ValueError as exc:
                    raise RpcError(-32001, "Plan approval failed", {"reason": "stale_spec_version"}) from exc

                async def prepare_and_run(sample_runner: JobRunner) -> None:
                    if sample_runner._state() is not JobState.plan_approved:
                        return
                    await sample_runner._worker(sample_runner.prepare_sample)
                    if (
                        sample_runner._stop_requested.is_set()
                        or sample_runner._state() is not JobState.sample_running
                    ):
                        return
                    await sample_runner.run_sample()

                await self.jobs.start(job, prepare_and_run)
                return self._job_status(job)
            if method == "job/approveSample":
                try:
                    approve_sample(self.store, job, "operator", cast(int, params["expectedSpecVersion"]))
                except TransitionError as exc:
                    raise RpcError(-32001, "Sample approval failed", {"reason": "invalid_job_state"}) from exc
                except ValueError as exc:
                    reason = "stale_spec_version"
                    raise RpcError(-32001, "Sample approval failed", {"reason": reason}) from exc
                return self._job_status(job)
            if method == "job/startFull":
                self._validate_job_operation(job, method)
                try:
                    start_full_run(self.store, job, "operator")
                except TransitionError as exc:
                    raise RpcError(-32001, "Full run failed", {"reason": "invalid_job_state"}) from exc

                async def start_full(full_runner: JobRunner) -> None:
                    if full_runner._state() is not JobState.full_running:
                        return
                    await full_runner.start_full_run()

                await self.jobs.start(job, start_full)
                return self._job_status(job)
            if method == "job/pause":
                self._validate_job_operation(job, method)
                try:
                    await self.jobs.pause(job)
                except TransitionError as exc:
                    raise RpcError(-32001, "Pause failed", {"reason": "invalid_job_state"}) from exc
                return self._job_status(job)
            if method == "job/resume":
                self._validate_job_operation(job, method)

                async def resume_paused(paused_runner: JobRunner) -> None:
                    if paused_runner._state() is not JobState.paused:
                        return
                    await paused_runner.resume("operator")

                await self.jobs.start(job, resume_paused)
                return self._job_status(job)
            if method == "job/rebuildRules":
                runner = self.jobs.runner(job)
                raw_rules = params.get("rules")
                if not isinstance(raw_rules, list):
                    raise RpcError(
                        -32602,
                        "Invalid params",
                        {"reason": "invalid_rules", "detail": "rules must be an array"},
                    )
                try:
                    from bca.outputs.rules import validate_rules
                    from bca.store.spec import _rule

                    spec, _ = load_latest_spec(runner.deps.job_dir)
                    rules = tuple(_rule(item) for item in raw_rules)
                    validate_rules(rules, spec.output_schema)
                except (TypeError, ValueError, KeyError) as exc:
                    raise RpcError(
                        -32602, "Invalid params", {"reason": "invalid_rules", "detail": str(exc)}
                    ) from exc
                path = await asyncio.to_thread(runner.rebuild_with_rules, rules)
                match = re.search(r"\.rebuild(\d+)\.xlsx$", str(path))
                rebuild_number = int(match.group(1)) if match else 0
                project_root = runner.deps.job_dir.parent.parent
                return {
                    "job": job,
                    "name": path.name,
                    "path": path.relative_to(project_root).as_posix(),
                    "rebuild": rebuild_number,
                }
            raise RpcError(-32601, "Method not found")
        except RpcError:
            raise
        except Exception as exc:
            raise _safe_error(exc) from exc

    def _running_job_ids(self, include_paused: bool = False) -> list[str]:
        states = [
            JobState.pulling,
            JobState.masking_sample,
            JobState.sample_running,
            JobState.full_running,
            JobState.quote_check,
            JobState.building_output,
            JobState.verifying,
        ]
        if include_paused:
            states.append(JobState.paused)
        placeholders = ",".join("?" for _ in states)
        return [
            str(row[0])
            for row in self.store.rows(
                f"SELECT id FROM jobs WHERE state IN ({placeholders})", tuple(state.value for state in states)
            )
        ]
