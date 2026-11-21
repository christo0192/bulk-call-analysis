from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import cast

import pytest

from bca.api.production import ProductionBackend
from bca.api.server import RpcError
from bca.bq.runner import DryRun
from bca.mcp.host import ToolHost
from bca.store import CapRule, JobSpec, LeadSourceIds, LeadSourceSql, UsableRule
from tests.api.test_production import make_backend
from tests.fakes.fake_bq import FakeBqRunner


def _rows() -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    leads: list[dict[str, object]] = [{"lead_id": str(1000 + index)} for index in range(30)]
    preflight: list[dict[str, object]] = []
    calls: list[dict[str, object]] = []
    for index, lead in enumerate(leads):
        lead_id = str(lead["lead_id"])
        has_transcript = index not in {1, 2}
        chars = 500 if index == 3 else 20
        preflight.append(
            {
                "lead_id": lead_id,
                "found": True,
                "calls": 1 if has_transcript else 0,
                "transcript_calls": 1 if has_transcript else 0,
                "usable_calls": 1 if has_transcript else 0,
                "transcription_failed": 0,
                "transcript_chars": chars,
            }
        )
        if has_transcript:
            calls.append(
                {
                    "lead_id": lead_id,
                    "call_id": f"550e8400-e29b-41d4-a716-44665544{int(lead_id):04d}",
                    "call_start": "2026-09-01T10:00:00+00:00",
                    "duration": 600_000,
                    "transcript_text": (
                        "my email is someone at example dot com. "
                        if lead_id == "1029"
                        else f"Synthetic transcript for lead {lead_id}; call 9876543210."
                    ),
                }
            )
    return leads, preflight, calls


def _host(backend: ProductionBackend) -> ToolHost:
    project_dir = backend._project_dir("project-one")
    return ToolHost(
        runner=backend._make_bq(),
        store=backend.store,
        project_dir=project_dir,
        bq_config=backend.config.bigquery,
        mask_context=backend._make_masker()(project_dir),
        project_id="project-one",
        recipe=backend.config.recipe,
        models=backend.config.models,
    )


async def _wait_state(backend: ProductionBackend, job: str, expected: set[str]) -> dict[str, object]:
    async def poll() -> dict[str, object]:
        while True:
            status = cast(dict[str, object], await backend.call("job/status", {"job": job}))
            if str(status["state"]) in expected:
                return status
            await asyncio.sleep(0.02)

    return await asyncio.wait_for(poll(), 60)


def test_production_job_lifecycle_from_mcp_to_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".bca-test-fakes").touch()
    monkeypatch.setenv("BCA_TEST_FAKES", "1")
    leads, preflight, calls = _rows()
    bq = FakeBqRunner(
        pages=[calls],
        preflight_pages=[preflight],
        pages_by_query_kind={"lead_source": [leads]},
        dry=DryRun(1, [], [("lead_id", "STRING")]),
    )
    backend = make_backend(tmp_path, bq=bq)

    async def scenario() -> None:
        await backend.call("projects/create", {"project": "project-one", "title": "Synthetic"})
        host = _host(backend)
        spec = JobSpec(
            1,
            "job-one",
            "project-one",
            "Synthetic job",
            "Synthetic question",
            LeadSourceSql("SELECT '1000' AS lead_id"),
            usable=UsableRule(min_chars=1),
            cap=CapRule(100, "drop_short_then_oldest", 180),
            instructions="Return JSON.",
            output_schema={"type": "object", "properties": {"score": {"type": "number"}}},
            luna_model=backend.config.models.luna_model,
            luna_effort=backend.config.models.luna_effort,
            sample_size=4,
        )
        card = cast(dict[str, object], host.call("propose_job", {"spec_json": json.dumps(spec.to_dict())}))
        job_dir = tmp_path / "projects" / "project-one" / "jobs" / "job-one"
        assert (job_dir / "spec.v1.json").is_file()
        (job_dir / "fake-codex-scenario.json").write_text(
            json.dumps({"default_answer": {"score": 1}, "models": [backend.config.models.luna_model]}),
            encoding="utf-8",
        )
        preflight_json = json.loads((job_dir / "preflight.v1.json").read_text(encoding="utf-8"))
        assert preflight_json["buckets"]["no_transcript"] == 2
        assert preflight_json["capped_leads"] == 1
        await backend.call(
            "job/approvePlan",
            {"job": "job-one", "expectedSpecVersion": cast(int, card["spec_version"])},
        )
        await _wait_state(backend, "job-one", {"sample_ready"})
        host.call("sample_results", {"job_id": "job-one"})
        transcript = cast(str, host.call("lead_transcript", {"job_id": "job-one", "ref": "S1"}))
        assert "[PHONE]" in transcript
        assert "9876543210" not in transcript
        await backend.call("job/approveSample", {"job": "job-one", "expectedSpecVersion": 1})
        await backend.call("job/startFull", {"job": "job-one"})
        await _wait_state(backend, "job-one", {"done", "done_with_issues"})
        held_back = backend.store.rows(
            "SELECT lead_id, reason FROM leads WHERE job_id=? AND status='held_back'", ("job-one",)
        )
        assert held_back == []
        seeded = backend.store.rows(
            "SELECT status FROM leads WHERE job_id=? AND lead_id=?", ("job-one", "1029")
        )
        assert [tuple(row) for row in seeded] == [("done",)]
        output = job_dir / "output"
        assert (output / "results.v1.xlsx").is_file()
        assert (output / "verify-report.v1.md").is_file()
        reconciliation = json.loads((output / "reconciliation.v1.json").read_text(encoding="utf-8"))
        assert reconciliation["adds_up"] is True
        assert sum(reconciliation["terms"].values()) == reconciliation["selected_distinct_ids"]
        listed = cast(list[dict[str, object]], await backend.call("files/list", {"project": "project-one"}))
        names = {str(item["name"]) for item in listed}
        assert "jobs/job-one/output/results.v1.xlsx" in names
        assert not any("/raw/" in name or "/masked/" in name for name in names)
        answers_before = list(backend.store.rows("SELECT lead_id, answer_json FROM results ORDER BY lead_id"))
        rule = {
            "kind": "threshold_group",
            "name": "score_group",
            "field": "score",
            "edges": [0],
            "labels": ["low", "high"],
        }
        rebuilt = await backend.call("job/rebuildRules", {"job": "job-one", "rules": [rule]})
        assert cast(dict[str, object], rebuilt)["rebuild"] == 1
        assert answers_before == list(
            backend.store.rows("SELECT lead_id, answer_json FROM results ORDER BY lead_id")
        )
        with pytest.raises(RpcError) as error:
            await backend.call("job/rebuildRules", {"job": "job-one", "rules": [{"kind": "bad"}]})
        assert error.value.code == -32602
        assert cast(dict[str, object], error.value.data)["reason"] == "invalid_rules"

    asyncio.run(scenario())


def test_id_list_preflight_numbers_are_persisted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".bca-test-fakes").touch()
    monkeypatch.setenv("BCA_TEST_FAKES", "1")
    preflight: list[dict[str, object]] = [
        {
            "lead_id": lead_id,
            "found": True,
            "calls": 1,
            "transcript_calls": 1,
            "usable_calls": 1,
            "transcription_failed": 0,
            "transcript_chars": 20,
        }
        for lead_id in ("1000", "1001")
    ]
    calls: list[dict[str, object]] = [
        {
            "lead_id": lead_id,
            "call_id": f"550e8400-e29b-41d4-a716-44665544{int(lead_id):04d}",
            "call_start": "2026-09-01T10:00:00+00:00",
            "duration": 600_000,
            "transcript_text": "Synthetic transcript",
        }
        for lead_id in ("1000", "1001")
    ]
    backend = make_backend(
        tmp_path,
        bq=FakeBqRunner(pages=[calls], preflight_pages=[preflight], dry=DryRun(1, [], [])),
    )

    async def scenario() -> None:
        await backend.call("projects/create", {"project": "project-one", "title": "Synthetic"})
        host = _host(backend)
        job_dir = tmp_path / "projects" / "project-one" / "jobs" / "job-ids"
        job_dir.mkdir(parents=True)
        (job_dir / "ids.txt").write_text("1000\n1000\nbad\n1001\n", encoding="utf-8")
        spec = JobSpec(
            1,
            "job-ids",
            "project-one",
            "ID job",
            "Synthetic question",
            LeadSourceIds("ids.txt", 4),
            usable=UsableRule(min_chars=1),
            cap=CapRule(100, "drop_short_then_oldest", 180),
            instructions="Return JSON.",
            output_schema={"type": "object", "properties": {}},
            luna_model=backend.config.models.luna_model,
            luna_effort=backend.config.models.luna_effort,
            sample_size=1,
        )
        host.call("propose_job", {"spec_json": json.dumps(spec.to_dict())})
        (job_dir / "fake-codex-scenario.json").write_text(
            json.dumps({"default_answer": {"ok": True}, "models": [backend.config.models.luna_model]}),
            encoding="utf-8",
        )
        record = json.loads((job_dir / "preflight.v1.json").read_text(encoding="utf-8"))
        assert record["id_intake"] == {"raw": 4, "distinct": 2, "duplicates": 1, "malformed": 1}
        await backend.call("job/approvePlan", {"job": "job-ids", "expectedSpecVersion": 1})
        await _wait_state(backend, "job-ids", {"sample_ready"})
        host.call("sample_results", {"job_id": "job-ids"})

    asyncio.run(scenario())
