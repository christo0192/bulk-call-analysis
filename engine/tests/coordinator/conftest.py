from __future__ import annotations

from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

import pytest

import bca.coordinator.runner as runner_module
from bca.bq.runner import DryRun
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
from bca.mask.masker import MaskingContext
from bca.mask.names import NerBackend, Span
from bca.monitor.events import Event, EventBus
from bca.store import CapRule, JobSpec, JobState, LeadSourceIds, Store, UsableRule, open_store, save_spec
from tests.fakes.fake_bq import FakeBqRunner


class FakeNer(NerBackend):
    def persons(self, text: str) -> list[Span]:
        marker = "Synthetic Name"
        start = text.find(marker)
        return [Span(start, start + len(marker))] if start >= 0 else []


@dataclass
class FakeClock:
    current: float = 1_700_000_000.0
    ticks: float = 0.0

    def now(self) -> float:
        return self.current

    def monotonic(self) -> float:
        return self.ticks

    def advance(self, seconds: float) -> None:
        self.current += seconds
        self.ticks += seconds


@dataclass
class EventRecorder:
    events: list[Event] = field(default_factory=list)

    def __call__(self, event: Event) -> None:
        self.events.append(event)


class RecordingEventBus(EventBus):
    def __init__(self, recorder: EventRecorder) -> None:
        super().__init__()
        self.recorder = recorder

    def publish(self, event: Event) -> None:
        self.recorder.events.append(event)
        super().publish(event)


@dataclass
class FakePoolControl:
    actions: dict[str, str] = field(default_factory=dict)
    attempts: dict[str, int] = field(default_factory=dict)
    calls: int = 0
    launches: int = 0
    used_percent: int = 0
    quota_increment: int = 0

    async def launch(self) -> Any:
        raise AssertionError("the deterministic pool must not launch a process")


class DeterministicPool:
    def __init__(
        self,
        _launch: object,
        callbacks: Any,
        _guard: object,
        concurrency: int,
        _lead_timeout: float,
        control: FakePoolControl,
        **_kwargs: object,
    ) -> None:
        self.callbacks = callbacks
        self.concurrency = concurrency
        self.control = control

    def set_concurrency(self, n: int) -> None:
        self.concurrency = max(1, n)

    async def stop(self) -> None:
        return

    async def run_until_idle(self) -> None:
        callbacks = self.callbacks
        while True:
            tasks = await callbacks.next_tasks(self.concurrency)
            if not tasks:
                return
            for task in tasks:
                action = self.control.actions.get(task.lead_id, "succeed")
                await callbacks.on_started(task)
                if action in {"fail", "usage_limit", "auth", "model_unavailable", "error_storm"}:
                    self.control.attempts[task.lead_id] = self.control.attempts.get(task.lead_id, 0) + 1
                    await callbacks.on_failure(
                        task, action, action not in {"usage_limit", "auth", "model_unavailable"}, action
                    )
                    if action in {"usage_limit", "auth", "model_unavailable", "error_storm"}:
                        await callbacks.on_pause(action if action != "usage_limit" else "quota", None)
                        return
                    if action == "fail" and self.control.attempts[task.lead_id] >= 2:
                        self.control.actions.pop(task.lead_id, None)
                else:
                    await callbacks.on_result(task, {"ok": True}, task.model, None)
                if action == "credit_signal":
                    await callbacks.on_pause("credit", None)
                    return
                self.control.calls += 1
                self.control.used_percent += self.control.quota_increment
                if self.control.used_percent >= 95:
                    await callbacks.on_pause("quota", 9999999999)
                    return


@dataclass(frozen=True)
class CoordinatorFixture:
    runner: JobRunner
    store: Store
    bq: FakeBqRunner
    clock: FakeClock
    events: EventRecorder
    pool: FakePoolControl
    config: SiteConfig
    job_dir: Path


def _config() -> SiteConfig:
    return SiteConfig(
        BigQueryConfig(project="synthetic-project"),
        RecipeConfig(
            "dataset.transcripts",
            "dataset.calls",
            "lead_id",
            "call_id",
            "transcript_text",
            "call_start",
            "duration",
            "s",
            60,
            180,
        ),
        ModelsConfig(luna_model="codex", luna_effort="low"),
        LimitsConfig(lead_timeout_s=2),
        RetentionConfig(),
        MaskingConfig(),
    )


def _spec(job_id: str) -> JobSpec:
    return JobSpec(
        1,
        job_id,
        "project",
        "Synthetic job",
        "Synthetic question",
        LeadSourceIds("ids.txt", 40),
        usable=UsableRule(min_chars=1),
        cap=CapRule(60, "drop_short_then_oldest", 180),
        instructions="Return the answer.",
        output_schema={
            "type": "object",
            "properties": {"ok": {"type": "boolean"}, "quote": {"type": "string"}},
        },
        luna_model="codex",
        luna_effort="low",
        sample_size=4,
    )


def _synthetic_rows() -> tuple[list[str], list[dict[str, object]], list[dict[str, object]]]:
    lead_ids = [str(index) for index in range(40)]
    no_transcript = {0, 1, 2}
    short = {3, 4, 5}
    cap = 6
    preflight: list[dict[str, object]] = []
    calls: list[dict[str, object]] = []
    for index, lead_id in enumerate(lead_ids):
        if index in no_transcript:
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
        count = 2 if index == cap else 1
        usable = 0 if index in short else 1
        preflight.append(
            {
                "lead_id": lead_id,
                "found": True,
                "calls": count,
                "usable_calls": usable,
                "transcript_calls": count,
                "transcription_failed": None,
            }
        )
        for call_index in range(count):
            calls.append(
                {
                    "lead_id": lead_id,
                    "call_id": f"call-{lead_id}-synthetic-{call_index}",
                    "transcript_text": f"Synthetic Name says hello. zzlead{lead_id}zz " + ("x" * 25),
                    "call_start": f"synthetic-start-{call_index}",
                    "duration": 30 if index in short or (index == cap and call_index == 0) else 200,
                }
            )
    return lead_ids, preflight, calls


@pytest.fixture
def coordinator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[CoordinatorFixture, None, None]:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    ids, preflight, calls = _synthetic_rows()
    (job_dir / "ids.txt").write_text("\n".join(ids) + "\n", encoding="utf-8", newline="\n")
    store = open_store(tmp_path / "store.db")
    store.create_project("project", "Synthetic")
    store.create_job("job", "project", 1, JobState.plan_proposed)
    save_spec(_spec("job"), job_dir)
    bq = FakeBqRunner(pages=[calls], dry=DryRun(1, [], []), preflight_pages=[preflight])
    clock, events, pool, config = FakeClock(), EventRecorder(), FakePoolControl(), _config()
    monkeypatch.setattr(runner_module, "LunaPool", partial(DeterministicPool, control=pool))
    bus = RecordingEventBus(events)
    subscription = bus.subscribe(10000)
    deps = RunnerDeps(
        store,
        bq,
        lambda *_: MaskingContext(allow_ids=frozenset(ids), ner=FakeNer()),
        pool.launch,
        clock,
        bus,
        config,
        job_dir,
        concurrency=4,
        approved_bytes=1,
        retry_backoff_s=0.01,
    )
    runner = JobRunner("job", deps)
    fixture = CoordinatorFixture(runner, store, bq, clock, events, pool, config, job_dir)
    try:
        yield fixture
    finally:
        bus.unsubscribe(subscription)
        store.close()


@pytest.fixture
def make_runner(coordinator: CoordinatorFixture) -> Callable[..., JobRunner]:
    def factory(**changes: Any) -> JobRunner:
        deps = coordinator.runner.deps
        for name, value in changes.items():
            deps = deps.__class__(**{**deps.__dict__, name: value})
        return JobRunner("job", deps)

    return factory
