from __future__ import annotations

import asyncio
import contextlib
import hashlib
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
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from bca import childenv
from bca.claude import launch as claude_launch
from bca.claude.auth import auth_status as claude_auth_status
from bca.claude.auth import start_login as start_claude_login
from bca.claude.profile import ClaudeProfile
from bca.claude.sessions import SessionStore
from bca.config import ConfigError, ModelsConfig, SiteConfig, load_config
from bca.coordinator.runner import Clock, JobRunner, RunnerDeps
from bca.luna.appserver import CodexLaunch
from bca.luna.appserver import start as start_app_server
from bca.luna.quota import parse_rate_limits
from bca.mask.masker import MaskingContext, mask_text
from bca.mask.names import SpacyNer
from bca.mcp.host import ToolHost, start_ipc_thread
from bca.mcp.ipc import new_ipc_address
from bca.monitor.events import Event, EventBus
from bca.monitor.progress import counts
from bca.monitor.sleep import SleepDetector
from bca.setup.appdirs import directories
from bca.setup.checklist import MANUAL, Checklist
from bca.stop import StopRequested
from bca.store import (
    JobState,
    PauseReason,
    Store,
    TransitionError,
    approve_plan,
    approve_sample,
    open_store,
    start_full_run,
    transition,
)

from .server import ApiBackend, RpcError

_SLUG = re.compile(r"^[a-z][a-z0-9-]{2,62}[a-z0-9]$")


class _RealClock:
    def now(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()


def _slug(value: object, field: str) -> str:
    if not isinstance(value, str) or _SLUG.fullmatch(value) is None:
        raise RpcError(-32602, "Invalid params", {"field": field, "reason": "invalid slug"})
    return value


class AppServerUnavailableError(ConnectionError):
    """The Codex app-server could not be started."""


class ClaudeTurnError(RuntimeError):
    """A Claude turn failed after the API accepted the request."""


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
        return RpcError(-32001, "Claude turn failed", {"reason": "claude_turn_failed"})
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
            raise RpcError(-32001, "Job is already running", {"job": job_id})

        async def invoke() -> None:
            rows = self.backend.store.rows("SELECT project_id FROM jobs WHERE id=?", (job_id,))
            if not rows:
                raise RpcError(-32001, "Job not found", {"job": job_id})
            runner = self.runner(job_id)
            await runner.wait_for_workers()
            runner.clear_stop()
            try:
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
        for job_id in list(self.tasks):
            await self.stop(job_id, timeout=1.0)
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
        concurrency: int = 4,
    ) -> None:
        configured_data_dir = os.environ.get("BCA_DATA_DIR")
        self.app_data = (
            app_data or (Path(configured_data_dir) if configured_data_dir else directories().app_data)
        ).expanduser()
        self.app_data.mkdir(parents=True, exist_ok=True)
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
        self.concurrency = concurrency
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
        self._chat_notify: Callable[[str, dict[str, object]], Awaitable[None]] | None = None
        self._login_processes: set[subprocess.Popen[str]] = set()
        self._chat_sessions_store = SessionStore(self.app_data / "claude" / "sessions.json")
        self._closed = False
        self._stopping = False
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
                command = (sys.executable, str(Path(fake_codex.__file__)), "--scenario", str(scenario))
                return await start_app_server(CodexLaunch(command, self.app_data / "codex", job_dir))
            executable = Path(os.environ.get("BCA_CODEX_BIN", "codex"))
            return await start_app_server(CodexLaunch(executable, self.app_data / "codex", job_dir))
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
        job_dir = self.app_data / "projects" / project_id / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        return RunnerDeps(
            self.store,
            self._make_bq(),
            lambda *_: self._make_masker()(job_dir),
            lambda: self._launch(job_dir),
            self.clock,
            self.event_bus,
            self.config,
            job_dir,
            self._ner,
            self.concurrency,
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
        return Path(os.environ.get("BCA_CLAUDE_BIN", "claude"))

    def _codex_binary(self) -> Path:
        return Path(os.environ.get("BCA_CODEX_BIN", "codex"))

    def _project_dir(self, project: str) -> Path:
        rows = self.store.rows("SELECT 1 FROM projects WHERE id=?", (project,))
        if not rows:
            raise RpcError(-32001, "Project not found", {"project": project})
        path = (self.app_data / "projects" / project).resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

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
        context = self._make_masker()(project_dir)
        masked_text = cast(str, self._masked(text_value, context))
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
        playbook_text = "You are assisting with the selected project.\n"
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
                if self._chat_notify is not None:
                    await self._chat_notify(project, {"kind": event.kind, "data": safe_event})
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
            raise ClaudeTurnError(str(exc)) from exc
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
        task = asyncio.create_task(self._chat_turn(project, text_value))
        self._chat_tasks[project] = task
        try:
            return await task
        except asyncio.CancelledError:
            if task in self._chat_stop_requests:
                return {"status": "stopped", "project": project}
            raise
        finally:
            if self._chat_tasks.get(project) is task:
                self._chat_tasks.pop(project, None)
            self._chat_stop_requests.discard(task)

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
        auth_dir = self.app_data / "auth-status"
        auth_dir.mkdir(parents=True, exist_ok=True)
        app_server = await self._launch(auth_dir)
        try:
            account = await app_server.client.request("account/read")
            limits = await app_server.client.request("account/rateLimits/read")
            model = self.config.models.luna_model if self.config else ModelsConfig().luna_model
            snapshot = parse_rate_limits(limits, model)
            return {
                "claude": {"loggedIn": claude.ok},
                "codex": {
                    "loggedIn": bool(account.get("account")),
                    "planType": snapshot.plan_type,
                    "usagePercent": snapshot.primary.used_percent if snapshot.primary else None,
                },
            }
        finally:
            await app_server.stop()

    async def _start_codex_login(self) -> dict[str, object]:
        env = childenv.build_child_env({"CODEX_HOME": str((self.app_data / "codex").resolve())})
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
        self._login_processes.add(subprocess.Popen([str(self._codex_binary()), "login"], **kwargs))
        return {"started": True, "vendor": "codex"}

    def _job_status(self, job_id: str) -> dict[str, object]:
        row = self.store.rows(
            "SELECT project_id, state, spec_version, pause_reason, resume_at FROM jobs WHERE id=?", (job_id,)
        )
        if not row:
            return {"job": job_id, "state": "unknown"}
        progress = counts(self.store, job_id)
        return {
            "job": job_id,
            "project": str(row[0][0]),
            "state": str(row[0][1]),
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
        }

    def _job_state(self, job_id: str) -> JobState:
        row = self.store.rows("SELECT state FROM jobs WHERE id=?", (job_id,))
        if not row:
            raise RpcError(-32001, "Job not found", {"job": job_id})
        return JobState(str(row[0][0]))

    def _publish_transition(self, job_id: str, before: str, after: str, reason: str | None) -> None:
        self.event_bus.publish(
            Event(
                "state",
                job_id,
                self.clock.now(),
                {"from": before, "to": after, "reason": reason, "source": "store"},
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
        return {
            "connectorPath": str(key) if key else None,
            "keyLocated": bool(key and key.is_file()),
            "workerCount": self.concurrency,
            "retentionDays": cfg.retention.masked_cache_days,
            "claudeModel": cfg.models.claude_model,
            "codexModel": cfg.models.luna_model,
        }

    @staticmethod
    def _is_internal_project_file(relative: Path) -> bool:
        parts = relative.parts
        name = relative.name
        if "chat" in parts or name == "harvested_names.json":
            return True
        if len(parts) >= 3 and parts[0] == "jobs" and parts[2] in {"raw", "masked"}:
            return True
        if name.startswith(".bca-") or name.endswith((".db", ".sqlite", ".sqlite3")):
            return True
        lowered = name.lower()
        return lowered.endswith(".log") and ("codex" in lowered or "app-server" in lowered)

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
            "workerCount",
            "retentionDays",
            "claudeModel",
            "codexModel",
            "confirmModelChange",
        }
        unknown = sorted(set(params) - allowed)
        if unknown:
            raise RpcError(-32602, "Invalid params", {"fields": unknown, "reason": "unknown field"})
        for key in ("connectorPath", "claudeModel", "codexModel"):
            if key in params and (not isinstance(params[key], str) or not params[key]):
                raise RpcError(-32602, "Invalid params", {"field": key, "reason": "must be non-empty"})
        claude = params.get("claudeModel")
        if claude is not None and (
            not isinstance(claude, str)
            or not claude.startswith("claude-")
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
            if key in params and (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 1
                or (key == "retentionDays" and value > 3650)
            ):
                raise RpcError(-32602, "Invalid params", {"field": key, "reason": "must be positive"})

    async def call(self, method: str, params: dict[str, object]) -> object:
        try:
            self._reload_config()
            if self.config is not None:
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
                self.store.close()
                self._closed = True
                return {"stopping": True, "exitCode": 0}
            if method == "setup/checklist":
                return [item.__dict__ for item in Checklist(self.app_data).items]
            if method == "setup/runChecks":
                checklist = Checklist(self.app_data)
                config = self.config
                probes = {
                    "uv_python": lambda: (sys.version_info >= (3, 12), sys.version.split()[0]),
                    "venv": lambda: (True, sys.executable),
                    "connector": lambda: (
                        config is not None and config.bigquery.key_path is not None,
                        "configured",
                    ),
                    "key": lambda: (
                        bool(
                            config is not None
                            and config.bigquery.key_path
                            and config.bigquery.key_path.is_file()
                        ),
                        "located",
                    ),
                    "claude_copy": lambda: (True, "available"),
                    "codex_copy": lambda: (shutil.which("codex") is not None, "codex"),
                    "spacy": lambda: (self._ner is not None or self._test_fakes_allowed(), "configured"),
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
                changes: dict[tuple[str, str], str] = {}
                if "claudeModel" in params:
                    changes[("models", "claude_model")] = json.dumps(params["claudeModel"])
                if "codexModel" in params:
                    changes[("models", "luna_model")] = json.dumps(params["codexModel"])
                if "connectorPath" in params:
                    changes[("bigquery", "key_path")] = json.dumps(params["connectorPath"])
                if "retentionDays" in params:
                    changes[("retention", "masked_cache_days")] = str(params["retentionDays"])
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
                if "workerCount" in params:
                    self.concurrency = cast(int, params["workerCount"])
                self.config_error = None
                self.jobs.recover()
                self.jobs.schedule_recovery()
                return self._settings()
            if method == "projects/list":
                self._require_config()
                return [
                    {"project": str(r[0]), "title": str(r[1])}
                    for r in self.store.rows("SELECT id,title FROM projects ORDER BY id")
                ]
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
                return [
                    {"name": p.relative_to(root).as_posix(), "size": p.stat().st_size}
                    for p in root.rglob("*")
                    if p.is_file() and not self._is_internal_project_file(p.relative_to(root))
                ]
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
            job = cast(str, params.get("job", ""))
            self._require_config()
            if method == "job/approvePlan":
                expected = cast(int, params["expectedSpecVersion"])
                old = self.jobs.tasks.get(job)
                if old is not None and not old.done():
                    raise RpcError(-32001, "Job is already running", {"job": job})
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
                rules = (
                    tuple(cast(list[object], params["rules"]))
                    if isinstance(params.get("rules"), list)
                    else ()
                )
                path = await asyncio.to_thread(runner.rebuild_with_rules, rules)
                return {"job": job, "path": str(path)}
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
