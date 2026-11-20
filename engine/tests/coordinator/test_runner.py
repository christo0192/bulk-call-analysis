from __future__ import annotations

from pathlib import Path
from typing import Any, cast

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
from bca.luna.appserver import AppServer
from bca.mask.masker import MaskingContext
from bca.monitor.events import EventBus
from bca.store import JobSpec, JobState, LeadSourceIds, Store, open_store, save_spec


def _spec(job_id: str) -> JobSpec:
    return JobSpec(
        1,
        job_id,
        "project",
        "Synthetic job",
        "Synthetic question",
        LeadSourceIds("ids.txt", 0),
        instructions="Return the answer.",
        output_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}},
        luna_model="codex",
        luna_effort="low",
        sample_size=1,
    )


def _config() -> SiteConfig:
    return SiteConfig(
        bigquery=BigQueryConfig(project="synthetic-project"),
        recipe=RecipeConfig(
            transcripts_table="dataset.transcripts",
            calls_table="dataset.calls",
            lead_id_column="lead_id",
            call_id_column="call_id",
            transcript_text_column="transcript_text",
            call_start_column="call_start",
            call_duration_column="duration",
        ),
        models=ModelsConfig(),
        limits=LimitsConfig(),
        retention=RetentionConfig(),
        masking=MaskingConfig(),
    )


async def _launch() -> AppServer:
    raise AssertionError("the recovery test must not launch a model")


def _runner(tmp_path: Path, store: Store, job_id: str) -> JobRunner:
    save_spec(_spec(job_id), tmp_path)
    deps = RunnerDeps(
        store=store,
        bq=cast(Any, object()),
        masking_context=lambda: MaskingContext(),
        launch_pool=cast(Any, _launch),
        clock=None,
        event_bus=EventBus(),
        config=_config(),
        job_dir=tmp_path,
    )
    return JobRunner(job_id, deps)


def test_recover_requeues_open_attempt_without_creating_a_duplicate(tmp_path: Path) -> None:
    store = open_store(tmp_path / "store.db")
    try:
        store.create_project("project", "Synthetic")
        store.create_job("job", "project", 1, JobState.sample_running)
        store.add_lead("job", "1", "queued")
        attempt = store.start_attempt("job", "1", "codex")
        runner = _runner(tmp_path, store, "job")

        runner.recover()

        lead = store.rows("SELECT status FROM leads WHERE job_id=? AND lead_id=?", ("job", "1"))
        attempts = store.rows("SELECT outcome, counted FROM attempts WHERE attempt_id=?", (attempt,))
        assert str(lead[0][0]) == "queued"
        assert str(attempts[0][0]) == "requeued"
        assert int(attempts[0][1]) == 0
    finally:
        store.close()


def test_runner_exposes_real_job_state_from_store(tmp_path: Path) -> None:
    store = open_store(tmp_path / "store.db")
    try:
        store.create_project("project", "Synthetic")
        store.create_job("job", "project", 1, JobState.sample_ready)
        runner = _runner(tmp_path, store, "job")
        assert runner._state() is JobState.sample_ready
    finally:
        store.close()
