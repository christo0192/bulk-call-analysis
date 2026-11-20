from __future__ import annotations

import asyncio
import gzip
import json
import os
import subprocess
import sys
import tracemalloc
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Any, Literal, cast

from openpyxl import load_workbook

from bca.config import (
    BigQueryConfig,
    LimitsConfig,
    MaskingConfig,
    ModelsConfig,
    RecipeConfig,
    RetentionConfig,
    SiteConfig,
)
from bca.coordinator import JobRunner, RunnerDeps
from bca.luna.appserver import AppServer, CodexLaunch, start
from bca.luna.pool import LunaPool
from bca.mask.leakscan import Hit, LeakReport
from bca.mask.masker import MaskingContext
from bca.monitor.events import EventBus
from bca.outputs.verify import VerifyReport, verify_job
from bca.store import JobSpec, JobState, LeadSourceIds, QuoteField, Store, open_store, reconcile, save_spec
from tests.fakes import fake_codex
from tests.fakes.fake_bq import FakeBqRunner


@dataclass(frozen=True)
class Population:
    normal: int = 10
    no_transcript: int = 0
    short_call_only: int = 0
    over_cap: int = 0
    pii: int = 0
    masker_hold_back: int = 0

    @property
    def total(self) -> int:
        return sum(
            (
                self.normal,
                self.no_transcript,
                self.short_call_only,
                self.over_cap,
                self.pii,
                self.masker_hold_back,
            )
        )


@dataclass(frozen=True)
class Fault:
    kind: str
    dispatch: int | None = None
    clock_time: float | None = None
    value: str | int | float | None = None
    lead: str | None = None


@dataclass(frozen=True)
class Scenario:
    name: str
    population: Population = field(default_factory=Population)
    source: Literal["sql", "ids"] = "ids"
    source_ids: tuple[str, ...] = ()
    concurrency: int = 2
    faults: tuple[Fault, ...] = ()
    expected_state: JobState = JobState.done
    expected_outcomes: dict[str, int] = field(default_factory=dict)
    expect: Callable[[SimResult], None] | None = None

    def __post_init__(self) -> None:
        if self.expect is None and self.faults:
            object.__setattr__(self, "expect", _fault_expectation(self.faults))


@dataclass
class SimResult:
    state: JobState
    outcome_counts: dict[str, int]
    reconciliation: Any
    attempts_per_lead: dict[str, int]
    uncounted_attempts_per_lead: dict[str, int]
    event_types: list[str]
    model_calls: int
    claude_calls: int
    wall_time: float
    peak_memory: int
    workbook: Path
    verify: VerifyReport
    store: Store
    prompt_log: Path | None = None
    planted_pii: dict[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass
class _Clock:
    value: float = 1_700_000_000.0
    mono_value: float = 0.0

    def now(self) -> float:
        return self.value

    def monotonic(self) -> float:
        return self.mono_value

    def advance_wall(self, seconds: float) -> None:
        self.value += seconds


class _Ner:
    def __init__(self, hold_back_ids: frozenset[str] = frozenset()) -> None:
        self.hold_back_ids = hold_back_ids

    def persons(self, text: str) -> list[Any]:
        from bca.mask.names import Span

        if any(f"zzlead{lead_id}zz" in text for lead_id in self.hold_back_ids):
            raise RuntimeError("synthetic masker hold-back")
        marker = "Synthetic Name"
        start = text.find(marker)
        return [Span(start, start + len(marker))] if start >= 0 else []


class _ProcessDeath(BaseException):
    """Synthetic process death that is not caught by callback error guards."""


_POOL_CALLS = 0
_POOL_PROMPTS: list[str] = []


def _config() -> SiteConfig:
    return SiteConfig(
        bigquery=BigQueryConfig(project="synthetic-project"),
        recipe=RecipeConfig(
            "dataset.transcripts",
            "dataset.calls",
            "lead_id",
            "call_id",
            "transcript_text",
            "call_start",
            "duration",
            "s",
            400_000,
            180,
        ),
        models=ModelsConfig(luna_model="codex", luna_effort="low"),
        limits=LimitsConfig(lead_timeout_s=2),
        retention=RetentionConfig(),
        masking=MaskingConfig(),
    )


def _make_rows(
    population: Population,
) -> tuple[list[str], list[dict[str, object]], list[dict[str, object]], dict[str, tuple[str, ...]]]:
    groups = (
        population.normal,
        population.no_transcript,
        population.short_call_only,
        population.over_cap,
        population.pii,
        population.masker_hold_back,
    )
    ids = [str(i) for i in range(sum(groups))]
    preflight: list[dict[str, object]] = []
    calls: list[dict[str, object]] = []
    planted_pii: dict[str, tuple[str, ...]] = {}
    offset = 0
    for group, count in enumerate(groups):
        for index in range(offset, offset + count):
            lead_id = ids[index]
            if group == 1:
                preflight.append(
                    {
                        "lead_id": lead_id,
                        "found": True,
                        "calls": 0,
                        "usable_calls": 0,
                        "transcript_calls": 0,
                        "transcription_failed": None,
                    }
                )
                continue
            usable = 0 if group == 2 else 1
            call_count = 2 if group == 3 else 1
            preflight.append(
                {
                    "lead_id": lead_id,
                    "found": True,
                    "calls": call_count,
                    "usable_calls": usable,
                    "transcript_calls": call_count,
                    "transcription_failed": None,
                }
            )
            for call_index in range(call_count):
                if group == 4:
                    text = (
                        f"Synthetic Name says hello. Phone 9876543210. Email pii-{lead_id}@example.test. "
                        f"zzlead{lead_id}zz"
                    )
                elif group == 5:
                    text = f"Synthetic Name says hello. PAN ABCDE1234F. zzlead{lead_id}zz"
                else:
                    text = f"Synthetic Name says hello. zzlead{lead_id}zz " + (
                        "x" * (20 if group != 2 else 2)
                    )
                calls.append(
                    {
                        "lead_id": lead_id,
                        "call_id": f"call-{lead_id}-synthetic-{call_index}",
                        "transcript_text": text,
                        "call_start": "synthetic-start",
                        "duration": 30 if group == 2 else 200,
                    }
                )
            tokens = ["Synthetic Name"]
            if group == 4:
                tokens.extend(("9876543210", f"pii-{lead_id}@example.test"))
            elif group == 5:
                tokens.append("ABCDE1234F")
            planted_pii[lead_id] = tuple(tokens)
        offset += count
    return ids, preflight, calls, planted_pii


def _spec(
    job_id: str, ids: list[str], population: Population, quote_fields: tuple[QuoteField, ...] = ()
) -> JobSpec:
    return JobSpec(
        1,
        job_id,
        "synthetic-project",
        "Synthetic job",
        "Synthetic question",
        LeadSourceIds("ids.txt", len(ids)),
        instructions="Return the answer.",
        output_schema={
            "type": "object",
            "properties": {"ok": {"type": "boolean"}, "quote": {"type": "string"}},
            "required": ["ok"],
        },
        quote_fields=quote_fields,
        luna_model="codex",
        luna_effort="low",
        sample_size=min(4, max(1, population.total)),
    )


async def _run(scenario: Scenario, root: Path) -> SimResult:
    import bca.coordinator.runner as runner_module_import

    runner_module: Any = runner_module_import
    global _POOL_CALLS
    _POOL_CALLS = 0
    _POOL_PROMPTS.clear()

    job_id = "scenario"
    job_dir = root / "job"
    job_dir.mkdir(parents=True)
    ids, preflight, calls, planted_pii = _make_rows(scenario.population)
    if scenario.name == "leak-holdbacks":
        # The independent leak scanner catches this synthetic value after masking.
        for call in calls:
            if str(call["lead_id"]) == "4":
                call["transcript_text"] = str(call["transcript_text"]) + " LEAK_SCAN_SENTINEL"
    if scenario.source_ids:
        known = {str(row["lead_id"]) for row in preflight}
        for value in scenario.source_ids:
            if value.isdigit() and value not in known:
                preflight.append(
                    {
                        "lead_id": value,
                        "found": False,
                        "calls": 0,
                        "usable_calls": 0,
                        "transcript_calls": 0,
                        "transcription_failed": None,
                    }
                )
    (job_dir / "ids.txt").write_text(
        "\n".join(scenario.source_ids or tuple(ids)) + "\n", encoding="utf-8", newline="\n"
    )
    store = open_store(root / "store.db")
    store.create_project("synthetic-project", "Synthetic")
    store.create_job(job_id, "synthetic-project", 1, JobState.plan_proposed)
    quote_fields = (
        (QuoteField("quote", "any"),)
        if any(fault.kind in {"quote_fail", "crash"} for fault in scenario.faults)
        else ()
    )
    save_spec(_spec(job_id, ids, scenario.population, quote_fields), job_dir)
    bq = FakeBqRunner(pages=[calls], preflight_pages=[preflight])
    bus, clock = EventBus(), _Clock()
    seen: list[str] = []
    events = bus.subscribe(100000)
    fake_scenario = _fake_scenario(scenario, ids)
    scenario_path = root / "fake-codex-scenario.json"
    scenario_path.write_text(json.dumps(fake_scenario), encoding="utf-8", newline="\n")

    async def launch() -> AppServer:
        return await start(
            CodexLaunch(
                (sys.executable, str(Path(fake_codex.__file__)), "--scenario", str(scenario_path)),
                root / "codex-home",
                root,
            )
        )

    original_pool = getattr(runner_module, "LunaPool")  # noqa: B009
    # Scenarios monkeypatch these class attributes; always restore them so no scenario leaks into the next.
    pristine_on_started = runner_module._Callbacks.on_started
    pristine_finish_output = runner_module.JobRunner._finish_output
    pristine_scan_lead = runner_module.scan_lead
    setattr(runner_module, "LunaPool", LunaPool)  # noqa: B010
    started = perf_counter()
    tracemalloc.start()
    try:
        if scenario.name == "leak-holdbacks":
            original_scan_lead = runner_module.scan_lead

            def scan_with_synthetic_leak(path: Path, allow_ids: frozenset[str]) -> LeakReport:
                report = cast(LeakReport, original_scan_lead(path, allow_ids))
                with gzip.open(path, "rt", encoding="utf-8") as masked_file:
                    contains_leak = "LEAK_SCAN_SENTINEL" in masked_file.read()
                if contains_leak:
                    return LeakReport([*report.hits, Hit("SYNTHETIC_LEAK", 0, 1)])
                return report

            runner_module.scan_lead = scan_with_synthetic_leak

        def new_deps() -> RunnerDeps:
            return RunnerDeps(
                store,
                bq,
                lambda *_: MaskingContext(
                    allow_ids=frozenset(ids),
                    ner=_Ner(
                        frozenset(ids[-scenario.population.masker_hold_back :])
                        if scenario.population.masker_hold_back
                        else frozenset()
                    ),
                ),
                launch,
                clock,
                bus,
                _config(),
                job_dir,
                concurrency=scenario.concurrency,
                approved_bytes=1,
            )

        async def discard_runner(current: JobRunner) -> None:
            pool: Any = current._phase_pool
            if pool is not None:
                try:
                    await asyncio.wait_for(pool.stop(), timeout=5)
                finally:
                    current._phase_pool = None

        deps = new_deps()
        runner = JobRunner(job_id, deps)
        crash_fault = next((fault for fault in scenario.faults if fault.kind == "crash"), None)
        original_started = runner_module._Callbacks.on_started if crash_fault else None
        crash_count = 0
        sleep_faults = [fault for fault in scenario.faults if fault.kind == "sleep_gap"]
        sleep_index = 0
        started_count = 0

        if sleep_faults:
            original_started_for_sleep = runner_module._Callbacks.on_started

            async def sleep_gap_started(callbacks: Any, task: Any) -> bool:
                nonlocal sleep_index, started_count
                started = bool(await original_started_for_sleep(callbacks, task))
                if started:
                    started_count += 1
                if (
                    started
                    and sleep_index < len(sleep_faults)
                    and (
                        sleep_faults[sleep_index].dispatch is None
                        or started_count == sleep_faults[sleep_index].dispatch
                    )
                ):
                    # Baseline heartbeat first: the detector compares against the previous one.
                    await callbacks.on_activity(clock.now())
                    clock.advance_wall(max(16.0, float(sleep_faults[sleep_index].value or 0)))
                    sleep_index += 1
                    await callbacks.on_activity(clock.now())
                return started

            runner_module._Callbacks.on_started = sleep_gap_started

        def install_crash_hook() -> None:
            nonlocal original_started
            original_started = runner_module._Callbacks.on_started

            async def crash_started(callbacks: Any, task: Any) -> None:
                nonlocal crash_count
                assert original_started is not None
                await original_started(callbacks, task)
                should_crash = crash_fault is not None and (
                    crash_fault.value != "repair"
                    or (task.repair and task.lead_id == _target(crash_fault, ids))
                )
                if should_crash and crash_count == 0:
                    crash_count += 1
                    raise _ProcessDeath()

            runner_module._Callbacks.on_started = crash_started

        if crash_fault and crash_fault.value == "sample":
            install_crash_hook()
        if any(fault.kind == "stale_spec" and fault.value == "plan" for fault in scenario.faults):
            try:
                runner.on_plan_approved(2)
            except ValueError:
                store.append_event(job_id, "gate_refused", {"gate": "plan", "reason": "stale"})
        if any(fault.kind == "automated_actor" and fault.value == "plan" for fault in scenario.faults):
            from bca.store import approve_plan

            try:
                approve_plan(store, job_id, "automatic", 1)  # type: ignore[arg-type]
            except Exception:
                store.append_event(job_id, "gate_refused", {"gate": "plan", "reason": "automatic"})
        runner.on_plan_approved(1)
        if any(fault.kind == "operator_pause" for fault in scenario.faults):
            from bca.store import PauseReason, pause

            pause(store, job_id, PauseReason.user)
            await runner.resume("operator")
        if any(fault.kind == "stdio_api" for fault in scenario.faults):
            return await _run_stdio_api(root, scenario, ids, preflight, calls)
        try:
            await runner.run_sample()
        except _ProcessDeath:
            if crash_fault is None:
                raise
            try:
                await discard_runner(runner)
            finally:
                runner_module._Callbacks.on_started = pristine_on_started
                runner_module.JobRunner._finish_output = pristine_finish_output
            runner = JobRunner(job_id, new_deps())
            recovered = runner.recover()
            assert recovered > 0
            store.append_event(job_id, "runner_recovered", {"requeued": recovered})
            if crash_fault.value == "recover":
                store.append_event(job_id, "synthetic_crash_after_recover", {})
                await discard_runner(runner)
                runner = JobRunner(job_id, new_deps())
                recovered = runner.recover()
                store.append_event(job_id, "runner_recovered", {"requeued": recovered})
            await runner.run_sample()
        while runner._state() is JobState.paused:
            reason = str(store.rows("SELECT pause_reason FROM jobs WHERE id=?", (job_id,))[0][0])
            if reason == "quota":
                clock.value = float(store.rows("SELECT resume_at FROM jobs WHERE id=?", (job_id,))[0][0])
                scenario_path.write_text(json.dumps({"latency_ms": 1}), encoding="utf-8", newline="\n")
                await runner.resume("automatic")
            else:
                _remove_active_fault(scenario_path, fake_scenario, reason)
                await runner.resume("operator")
        if runner._state() is not JobState.sample_ready:
            raise AssertionError(f"sample did not complete: {runner._state()}")
        if any(fault.kind == "stale_spec" and fault.value == "sample" for fault in scenario.faults):
            try:
                runner.on_sample_approved(2)
            except ValueError:
                store.append_event(job_id, "gate_refused", {"gate": "sample", "reason": "stale"})
        if any(fault.kind == "automated_actor" and fault.value == "sample" for fault in scenario.faults):
            from bca.store import approve_sample

            try:
                approve_sample(store, job_id, "automatic", 1)  # type: ignore[arg-type]
            except Exception:
                store.append_event(job_id, "gate_refused", {"gate": "sample", "reason": "automatic"})
        runner.on_sample_approved(1)
        if any(fault.kind == "stale_spec" and fault.value == "full" for fault in scenario.faults):
            from bca.store import start_full_run as start_full_run_gate

            try:
                start_full_run_gate(store, job_id, "operator", 2)
            except Exception:
                store.append_event(job_id, "gate_refused", {"gate": "full", "reason": "stale"})
        if any(fault.kind == "automated_actor" and fault.value == "full" for fault in scenario.faults):
            from bca.store import start_full_run as start_full_run_gate

            try:
                start_full_run_gate(store, job_id, "automatic", 1)  # type: ignore[arg-type]
            except Exception:
                store.append_event(job_id, "gate_refused", {"gate": "full", "reason": "automatic"})
        if crash_fault and crash_fault.value in {"full", "repair", "recover"}:
            install_crash_hook()
        if crash_fault and crash_fault.value == "output":
            original_finish = runner_module.JobRunner._finish_output

            async def crash_finish(current: JobRunner, state: JobState) -> None:
                runner_module.JobRunner._finish_output = original_finish
                raise _ProcessDeath()

            runner_module.JobRunner._finish_output = crash_finish
        try:
            await runner.start_full_run()
        except _ProcessDeath:
            if crash_fault is None:
                raise
            try:
                await discard_runner(runner)
            finally:
                runner_module._Callbacks.on_started = pristine_on_started
                runner_module.JobRunner._finish_output = pristine_finish_output
            runner = JobRunner(job_id, new_deps())
            recovered = runner.recover()
            store.append_event(job_id, "runner_recovered", {"requeued": recovered})
            if crash_fault is not None and crash_fault.value == "recover":
                crash_count = 0
                install_crash_hook()
                try:
                    await runner.start_full_run()
                except _ProcessDeath:
                    await discard_runner(runner)
                    runner_module._Callbacks.on_started = pristine_on_started
                    runner = JobRunner(job_id, new_deps())
                    recovered = runner.recover()
                    store.append_event(job_id, "runner_recovered", {"requeued": recovered})
                    await runner.start_full_run()
            else:
                await runner.start_full_run()
        while runner._state() is JobState.paused:
            _remove_active_fault(
                scenario_path,
                fake_scenario,
                str(store.rows("SELECT pause_reason FROM jobs WHERE id=?", (job_id,))[0][0]),
            )
            await runner.resume("operator")
        for fault in scenario.faults:
            if fault.kind == "rebuild":
                runner.rebuild_with_rules(())
        path = job_dir / "output" / "results.v1.xlsx"
        report = verify_job(store, job_id, path)
        state = JobState(str(store.rows("SELECT state FROM jobs WHERE id=?", (job_id,))[0][0]))
        event_rows = store.rows("SELECT kind FROM job_events WHERE job_id=? ORDER BY id", (job_id,))
        seen.extend(str(row[0]) for row in event_rows)
        bus_events = events.drain()
        seen.extend(event.kind for event in bus_events)
        outcome_rows = store.rows(
            "SELECT status, COUNT(*) FROM leads WHERE job_id=? GROUP BY status", (job_id,)
        )
        outcomes = {str(k): int(v) for k, v in outcome_rows}
        attempts = store.rows(
            "SELECT lead_id, COUNT(*) FROM attempts WHERE job_id=? AND counted=1 GROUP BY lead_id", (job_id,)
        )
        uncounted = store.rows(
            "SELECT lead_id, COUNT(*) FROM attempts WHERE job_id=? AND counted=0 GROUP BY lead_id", (job_id,)
        )
        peak = tracemalloc.get_traced_memory()[1]
        result = SimResult(
            state,
            outcomes,
            reconcile(store, job_id, final=True),
            {str(k): int(v) for k, v in attempts},
            {str(k): int(v) for k, v in uncounted},
            seen,
            int(store.rows("SELECT COUNT(*) FROM attempts WHERE job_id=?", (job_id,))[0][0]),
            0,
            perf_counter() - started,
            peak,
            path,
            report,
            store,
            root / "fake-codex-prompts.jsonl",
            planted_pii,
        )
        if scenario.expect is not None:
            scenario.expect(result)
        return result
    finally:
        tracemalloc.stop()
        setattr(runner_module, "LunaPool", original_pool)  # noqa: B010
        runner_module._Callbacks.on_started = pristine_on_started
        runner_module.JobRunner._finish_output = pristine_finish_output
        runner_module.scan_lead = pristine_scan_lead


def _target(fault: Fault, ids: list[str]) -> str:
    if fault.lead:
        return fault.lead.removeprefix("lead-").lstrip("0") or "0"
    index = max(0, (fault.dispatch or 1) - 1)
    return ids[min(index, len(ids) - 1)] if ids else "0"


def _fake_scenario(scenario: Scenario, ids: list[str]) -> dict[str, object]:
    value: dict[str, object] = {"latency_ms": 1}
    errors: dict[str, str] = {}
    answers: dict[str, object] = {}
    for fault in scenario.faults:
        lead = _target(fault, ids)
        if fault.kind == "usage_limit":
            value.update({"used_percent": 94, "quota_increment": 1})
        elif fault.kind == "credit_signal":
            value["credit_drop"] = True
        elif fault.kind in {"reroute"}:
            value.update(
                {
                    "reroute": lead,
                    "reroute_once": True,
                    "models": ["codex", str(fault.value or "codex-mini")],
                }
            )
        elif fault.kind in {"error", "auth_lost", "model_unavailable", "error_storm"}:
            code = {"auth_lost": "unauthorized", "model_unavailable": "modelNotFound"}.get(
                fault.kind, str(fault.value or "serverOverloaded")
            )
            targets = ids if fault.kind == "error_storm" else [lead]
            errors.update({target: code for target in targets})
        elif fault.kind == "latency":
            value["latency_ms"] = int(fault.value or 1)
        elif fault.kind == "stall":
            value["hang"] = lead
        elif fault.kind == "forbidden_item":
            value.update({"tool_call": lead, "tool_call_type": str(fault.value or "commandExecution")})
        elif fault.kind == "quote_fail" and not any(
            other.kind == "schema_invalid" and other.lead == fault.lead for other in scenario.faults
        ):
            answers[lead] = {"ok": True, "quote": "bad"}
            value.setdefault("answers_after_first", {})
            cast(dict[str, object], value["answers_after_first"])[lead] = {
                "ok": True,
                "quote": "Synthetic Name says hello.",
            }
        elif fault.kind == "schema_invalid" or fault.kind == "quote_fail":
            answers[lead] = "not-json"
        elif fault.kind == "crash" and fault.value == "repair":
            answers[lead] = {"ok": True, "quote": "bad"}
            value.setdefault("answers_after_first", {})
            cast(dict[str, object], value["answers_after_first"])[lead] = {
                "ok": True,
                "quote": "Synthetic Name says hello.",
            }
    if errors:
        value["errors"] = errors
    if answers:
        value["answers"] = answers
    return value


def _remove_active_fault(path: Path, scenario: dict[str, object], reason: str) -> None:
    updated = dict(scenario)
    if reason == "credit":
        updated.pop("credit_drop", None)
    elif reason in {"auth", "model_unavailable", "error_storm", "network"}:
        updated.pop("errors", None)
    path.write_text(json.dumps(updated), encoding="utf-8", newline="\n")


async def _run_stdio_api(
    root: Path,
    scenario: Scenario,
    ids: list[str],
    preflight: list[dict[str, object]],
    calls: list[dict[str, object]],
) -> SimResult:
    data_dir = root / "stdio-data"
    job_dir = data_dir / "projects" / "synthetic-project" / "scenario"
    data_dir.mkdir()
    (data_dir / ".bca-test-fakes").touch()
    (data_dir / "site.toml").write_text(
        """[bigquery]
project = "synthetic-project"

[recipe]
transcripts_table = "dataset.transcripts"
calls_table = "dataset.calls"
lead_id_column = "lead_id"
call_id_column = "call_id"
transcript_text_column = "transcript_text"
call_start_column = "call_start"
call_duration_column = "duration"
duration_unit = "s"
max_chars_per_lead = 400000
drop_calls_shorter_than_s = 180

[models]
luna_model = "codex"
luna_effort = "low"

[limits]
lead_timeout_s = 2

[retention]
masked_cache_days = 30
chat_days = 90

[masking]
""",
        encoding="utf-8",
        newline="\n",
    )
    (data_dir / "fake-bq.json").write_text(
        json.dumps({"pages": [calls], "preflight_pages": [preflight]}), encoding="utf-8", newline="\n"
    )
    child_env = {
        key: value
        for key, value in os.environ.items()
        if "API_KEY" not in key and key not in {"OPENAI_API_KEY", "CODEX_HOME"}
    }
    child_env.update(
        {
            "BCA_DATA_DIR": str(data_dir),
            "BCA_CONFIG": str(data_dir / "site.toml"),
            "BCA_TEST_FAKES": "1",
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
        }
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "bca.api.stdio"],
        cwd=root,
        env=child_env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    request_id = 0
    deadline = perf_counter() + 120

    async def request(method: str, params: dict[str, object]) -> dict[str, object]:
        nonlocal request_id
        if perf_counter() >= deadline:
            raise TimeoutError("stdio API scenario exceeded 120 seconds")
        request_id += 1
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(
            json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}) + "\n"
        )
        process.stdin.flush()
        while True:
            line = process.stdout.readline()
            if not line:
                stderr = process.stderr.read() if process.stderr is not None else ""
                raise AssertionError(f"stdio API exited early: {stderr}")
            response = cast(dict[str, object], json.loads(line))
            if response.get("id") == request_id:
                if "error" in response:
                    raise AssertionError(response["error"])
                return response

    try:
        async with asyncio.timeout(120):
            await request("app/status", {})
            await request("projects/create", {"project": "synthetic-project", "title": "Synthetic"})
            from bca.store import open_store

            job_dir.mkdir(parents=True)
            (job_dir / "fake-codex-scenario.json").write_text(
                json.dumps(_fake_scenario(scenario, ids)), encoding="utf-8", newline="\n"
            )
            setup_store = open_store(data_dir / "store.sqlite3")
            try:
                setup_store.create_job("scenario", "synthetic-project", 1, JobState.plan_proposed)
                (job_dir / "ids.txt").write_text("\n".join(ids) + "\n", encoding="utf-8", newline="\n")
                save_spec(_spec("scenario", ids, scenario.population), job_dir)
            finally:
                setup_store.close()
            await request("job/approvePlan", {"job": "scenario", "expectedSpecVersion": 1})
            while True:
                if perf_counter() >= deadline:
                    raise TimeoutError("stdio API sample exceeded 120 seconds")
                status = await request("job/status", {"job": "scenario"})
                result = cast(dict[str, object], status["result"])
                state = result["state"]
                if state == JobState.sample_ready.value:
                    break
                assert state not in {JobState.done.value, JobState.done_with_issues.value}
                await asyncio.sleep(0.05)
            await request("job/approveSample", {"job": "scenario", "expectedSpecVersion": 1})
            await request("job/startFull", {"job": "scenario"})
            while True:
                if perf_counter() >= deadline:
                    raise TimeoutError("stdio API full run exceeded 120 seconds")
                status = await request("job/status", {"job": "scenario"})
                result = cast(dict[str, object], status["result"])
                state = result["state"]
                if state in {JobState.done.value, JobState.done_with_issues.value}:
                    break
                await asyncio.sleep(0.05)
            await request("shutdown", {})
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()

    store = open_store(data_dir / "store.sqlite3")
    path = job_dir / "output" / "results.v1.xlsx"
    report = verify_job(store, "scenario", path)
    state = JobState(str(store.rows("SELECT state FROM jobs WHERE id=?", ("scenario",))[0][0]))
    attempts = store.rows(
        "SELECT lead_id, COUNT(*) FROM attempts WHERE job_id=? AND counted=1 GROUP BY lead_id", ("scenario",)
    )
    return SimResult(
        state,
        {},
        reconcile(store, "scenario", final=True),
        {str(k): int(v) for k, v in attempts},
        {},
        [
            str(row[0])
            for row in store.rows("SELECT kind FROM job_events WHERE job_id=? ORDER BY id", ("scenario",))
        ],
        int(store.rows("SELECT COUNT(*) FROM attempts WHERE job_id=?", ("scenario",))[0][0]),
        0,
        0,
        0,
        path,
        report,
        store,
    )


def _fault_expectation(faults: tuple[Fault, ...]) -> Callable[[SimResult], None]:
    """Return persisted, fault-specific checks; never treat the scenario input as evidence."""

    def expect(result: SimResult) -> None:
        rows = result.store.rows("SELECT kind, data_json FROM job_events WHERE job_id=?", ("scenario",))
        events = [(str(kind), str(data)) for kind, data in rows]
        for fault in faults:
            if fault.kind == "usage_limit":
                assert any('"quota"' in data for kind, data in events if kind == "state")
                assert any(count == 1 for count in result.attempts_per_lead.values())
            elif fault.kind == "credit_signal":
                assert any('"credit"' in data for kind, data in events if kind == "state")
            elif fault.kind == "reroute":
                target = _target(
                    fault,
                    [
                        str(row[0])
                        for row in result.store.rows(
                            "SELECT lead_id FROM leads WHERE job_id=? ORDER BY lead_id", ("scenario",)
                        )
                    ],
                )
                reroutes = result.store.rows(
                    "SELECT data_json FROM job_events WHERE job_id=? AND kind='reroute'", ("scenario",)
                )
                assert reroutes
                assert '"from_model": "codex"' in str(reroutes[0][0])
                assert f'"to_model": "{fault.value or "codex-mini"}"' in str(reroutes[0][0])
                attempt = result.store.rows(
                    "SELECT model_used, rerouted_from FROM attempts "
                    "WHERE job_id=? AND lead_id=? AND model_used IS NOT NULL LIMIT 1",
                    ("scenario", target),
                )[0]
                assert tuple(str(value) for value in attempt) == (str(fault.value or "codex-mini"), "codex")
            elif fault.kind in {"auth_lost", "model_unavailable", "error_storm", "error"}:
                assert (
                    any(
                        f'"{fault.kind if fault.kind != "error" else "network"}"' in data
                        for kind, data in events
                        if kind == "state"
                    )
                    or result.uncounted_attempts_per_lead
                )
            elif fault.kind == "sleep_gap":
                assert any('"sleep"' in data for kind, data in events if kind == "state")
                assert any('"resumed"' in data for kind, data in events if kind == "state")
                assert result.uncounted_attempts_per_lead
            elif fault.kind == "operator_pause":
                assert any('"user"' in data for kind, data in events if kind == "state")
            elif fault.kind in {"quote_fail", "schema_invalid"}:
                assert (
                    result.model_calls
                    > result.store.rows("SELECT COUNT(*) FROM leads WHERE job_id=?", ("scenario",))[0][0]
                )
            elif fault.kind == "latency":
                assert result.model_calls > 0
            elif fault.kind == "crash":
                assert result.state in {JobState.done, JobState.done_with_issues}
                assert result.attempts_per_lead
                assert result.reconciliation.ok, result.reconciliation.problems
                result_rows = result.store.rows(
                    "SELECT lead_id, COUNT(*) FROM results WHERE job_id=? GROUP BY lead_id", ("scenario",)
                )
                assert all(int(count) == 1 for _, count in result_rows)
                if fault.value == "recover":
                    assert sum(kind == "runner_recovered" for kind, _ in events) == 2
            elif fault.kind == "stall":
                target = _target(
                    fault,
                    [
                        str(row[0])
                        for row in result.store.rows(
                            "SELECT lead_id FROM leads WHERE job_id=? ORDER BY lead_id", ("scenario",)
                        )
                    ],
                )
                timeout_attempts = result.store.rows(
                    "SELECT counted, error_code FROM attempts WHERE job_id=? AND lead_id=? "
                    "AND error_code='timeout'",
                    ("scenario", target),
                )
                assert any(int(counted) == 1 for counted, _error_code in timeout_attempts) or (
                    result.attempts_per_lead.get(target, 0) > 1
                )
                assert result.state in {JobState.done, JobState.done_with_issues}
            elif fault.kind == "rebuild":
                assert "rules_rebuilt" in result.event_types
            elif fault.kind in {"stale_spec", "automated_actor"}:
                assert "gate_refused" in result.event_types
            elif fault.kind == "stdio_api":
                assert result.state in {JobState.done, JobState.done_with_issues}
                assert result.attempts_per_lead
                assert result.model_calls > 0
                assert result.workbook.is_file()
                assert result.verify.ok

    return expect


def run_scenario(scenario: Scenario, tmp_path: Path) -> SimResult:
    """Drive a complete synthetic job through the real coordinator APIs."""
    return asyncio.run(_run(scenario, tmp_path))


def assert_invariants(result: SimResult) -> None:
    assert result.reconciliation.ok, result.reconciliation.problems
    assert result.verify.ok, result.verify
    assert result.workbook.is_file()
    workbook = load_workbook(result.workbook, data_only=False, read_only=True)
    try:
        assert all(
            cell.data_type != "f"
            for sheet in workbook.worksheets
            for row in sheet.iter_rows()
            for cell in row
        )
    finally:
        workbook.close()
    if result.prompt_log is not None and result.prompt_log.is_file():
        prompts = result.prompt_log.read_text(encoding="utf-8")
        prompted_leads = {
            str(lead_id)
            for (lead_id,) in result.store.rows(
                "SELECT DISTINCT lead_id FROM attempts WHERE job_id=?", ("scenario",)
            )
        }
        tokens = {
            token
            for lead_id, lead_tokens in result.planted_pii.items()
            if lead_id in prompted_leads
            for token in lead_tokens
        }
        tokens.add("LEAK_SCAN_SENTINEL")
        for token in tokens:
            assert token not in prompts, f"synthetic PII leaked into a model prompt: {token}"
