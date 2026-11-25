from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, cast

import pytest

import bca.api.production as production
from bca.api.production import ProductionBackend
from bca.api.server import RpcError
from bca.bq.runner import DryRun
from bca.claude.auth import AuthStatus
from bca.mcp.host import ToolHost
from bca.store import CapRule, JobSpec, JobState, LeadSourceIds, UsableRule, open_store
from bca.store.lifecycle import transition
from tests.api.test_production import _write_job, make_backend, write_valid_config
from tests.api.test_production_chat import allow_fakes
from tests.fakes.fake_bq import FakeBqRunner


def run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


def allow_claude_fake(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, scenario: str = "normal") -> None:
    # The Claude child process gets an allowlisted environment, so a scenario set with
    # monkeypatch.setenv never reaches the fake. Bake it into the launcher, as
    # tests/api/test_production_chat.py::allow_fakes does.
    allow_fakes(monkeypatch, tmp_path, {"FAKE_CLAUDE_SCENARIO": scenario})


def create_project(
    backend: ProductionBackend, project: str = "project-one", title: str = "Synthetic"
) -> None:
    run(backend.call("projects/create", {"project": project, "title": title}))


def test_projects_list_has_created_at_active_and_last_job(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    create_project(backend)
    backend.store.create_job("job-one", "project-one", 1, "full_running", "Job title")
    listed = cast(list[dict[str, object]], run(backend.call("projects/list", {})))
    project = listed[0]
    assert isinstance(project["createdAt"], int)
    assert project["activeJob"] == project["lastJob"]
    assert cast(dict[str, object], project["lastJob"])["title"] == "Job title"


def test_projects_jobs_newest_first_with_public_state_and_title(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    create_project(backend)
    backend.store.create_job("job-old", "project-one", 1, "draft", "Old title")
    backend.store.create_job("job-new", "project-one", 1, "plan_proposed", "Title from spec")
    jobs = cast(dict[str, object], run(backend.call("projects/jobs", {"project": "project-one"})))["jobs"]
    assert [cast(dict[str, object], job)["job"] for job in cast(list[object], jobs)] == ["job-new", "job-old"]
    assert cast(dict[str, object], cast(list[object], jobs)[0])["state"] == "planned"
    assert cast(dict[str, object], cast(list[object], jobs)[0])["title"] == "Title from spec"


def test_active_job_skips_finished_jobs(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    create_project(backend)
    backend.store.create_job("job-done", "project-one", 1, "pulling", "Done")
    transition(backend.store, "job-done", JobState.failed)
    backend.store.create_job("job-live", "project-one", 1, "draft", "Live")
    project = cast(list[dict[str, object]], run(backend.call("projects/list", {})))[0]
    assert cast(dict[str, object], project["activeJob"])["job"] == "job-live"


def test_finished_at_written_on_terminal_states(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    backend.store.create_project("project-one", "Synthetic")
    backend.store.create_job("job-one", "project-one", 1, "pulling")
    transition(backend.store, "job-one", JobState.failed)
    assert backend.store.rows("SELECT finished_at FROM jobs WHERE id=?", ("job-one",))[0][0] is not None


def test_projects_jobs_unknown_project_is_project_not_found(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    with pytest.raises(RpcError) as error:
        run(backend.call("projects/jobs", {"project": "missing"}))
    assert cast(dict[str, object], error.value.data)["reason"] == "project_not_found"


def test_jobs_title_column_migrates_an_old_schema_database(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite3"
    store = open_store(path)
    store.connection.execute("PRAGMA user_version=5")
    store.close()
    connection = __import__("sqlite3").connect(path)
    connection.execute("ALTER TABLE jobs DROP COLUMN title")
    connection.execute("PRAGMA user_version=5")
    connection.commit()
    connection.close()
    migrated = open_store(path)
    assert any(str(row[1]) == "title" for row in migrated.connection.execute("PRAGMA table_info(jobs)"))
    migrated.close()


def test_chat_history_records_user_assistant_and_tool_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    allow_claude_fake(monkeypatch, tmp_path, "tool_call")
    backend = make_backend(tmp_path, bq=FakeBqRunner(pages=[[{"phone": "Call 9876543210"}]]))
    create_project(backend)
    run(backend.call("chat/send", {"project": "project-one", "text": "phone 9876543210"}))
    backend._append_chat_entry("project-one", {"turn": "synthetic", "role": "assistant", "text": "done"})
    entries = cast(dict[str, object], run(backend.call("chat/history", {"project": "project-one"})))[
        "entries"
    ]
    values = cast(list[dict[str, object]], entries)
    assert [entry["role"] for entry in values] == ["user", "tool", "tool", "assistant"]
    assert values[0]["text"] == "phone [PHONE]"
    assert all("9876543210" not in json.dumps(entry) for entry in values)


def test_chat_history_assistant_text_is_not_duplicated_by_deltas(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    create_project(backend)
    path = backend._chat_history_path("project-one")
    path.write_text(
        json.dumps(
            {"id": 1, "ts": int(backend.clock.now()), "turn": "t", "role": "assistant", "text": "hello"}
        )
        + "\n",
        encoding="utf-8",
    )
    result = cast(dict[str, object], run(backend.call("chat/history", {"project": "project-one"})))
    assert result["entries"] == [json.loads(path.read_text(encoding="utf-8"))]


def test_chat_history_paging_with_before_and_limit(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    create_project(backend)
    path = backend._chat_history_path("project-one")
    path.write_text(
        "".join(
            json.dumps({"id": i, "ts": int(backend.clock.now()), "role": "user", "text": str(i)}) + "\n"
            for i in range(1, 5)
        ),
        encoding="utf-8",
    )
    result = cast(
        dict[str, object],
        run(backend.call("chat/history", {"project": "project-one", "before": 4, "limit": 2})),
    )
    assert [entry["id"] for entry in cast(list[dict[str, object]], result["entries"])] == [2, 3]
    assert result["more"] is True


def test_chat_history_ignores_a_partial_last_line(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    create_project(backend)
    backend._chat_history_path("project-one").write_text(
        json.dumps({"id": 1, "ts": int(backend.clock.now())}) + '\n{"id":2', encoding="utf-8"
    )
    entries = cast(dict[str, object], run(backend.call("chat/history", {"project": "project-one"})))[
        "entries"
    ]
    assert [entry["id"] for entry in cast(list[dict[str, object]], entries)] == [1]


def test_chat_events_carry_turn_and_entry_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    allow_claude_fake(monkeypatch, tmp_path)
    backend = make_backend(tmp_path)
    create_project(backend)
    events: list[dict[str, object]] = []

    async def record(_project: str, event: dict[str, object]) -> None:
        events.append(event)

    backend.set_chat_notify(record)
    run(backend.call("chat/send", {"project": "project-one", "text": "hello"}))
    assert events and all(
        isinstance(event["turn"], str) and isinstance(event["entryId"], int) for event in events
    )


def test_chat_history_tool_summary_never_contains_raw_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    allow_claude_fake(monkeypatch, tmp_path, "tool_call")
    backend = make_backend(tmp_path, bq=FakeBqRunner(pages=[[{"phone": "RAW-SYNTHETIC-OUTPUT"}]]))
    create_project(backend)
    run(backend.call("chat/send", {"project": "project-one", "text": "query"}))
    serialized = json.dumps(run(backend.call("chat/history", {"project": "project-one"})))
    assert "RAW-SYNTHETIC-OUTPUT" not in serialized


def test_files_list_has_modified_and_kind_newest_first(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    create_project(backend)
    root = backend._project_dir("project-one")
    (root / "brief.md").write_text("brief", encoding="utf-8")
    (root / "summary.md").write_text("summary", encoding="utf-8")
    # Two writes can land on the same file-system tick, so set the times explicitly.
    os.utime(root / "brief.md", (1_700_000_000, 1_700_000_000))
    os.utime(root / "summary.md", (1_700_000_100, 1_700_000_100))
    listed = cast(list[dict[str, object]], run(backend.call("files/list", {"project": "project-one"})))
    assert listed[0]["name"] == "summary.md"
    assert all("modified" in item and "kind" in item for item in listed)


def test_files_path_returns_absolute_path_and_project_dir(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    create_project(backend)
    (backend._project_dir("project-one") / "brief.md").write_text("x", encoding="utf-8")
    result = cast(
        dict[str, object], run(backend.call("files/path", {"project": "project-one", "name": "brief.md"}))
    )
    assert Path(str(result["path"])).is_absolute() and result["projectDir"] == str(
        backend._project_dir("project-one")
    )


def test_files_path_rejects_traversal_hidden_and_internal_names(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    create_project(backend)
    root = backend._project_dir("project-one")
    internal = (
        "jobs/job-one/raw/1.jsonl.gz",
        "jobs/job-one/masked/1.jsonl.gz",
        "jobs/job-one/harvested_names.json",
        "chat/history.jsonl",
        # Windows ignores case in file names, so an upper-case spelling is the same internal file.
        "jobs/job-one/RAW/2.jsonl.gz",
        "Chat/Notes.txt",
    )
    # The internal files exist, so only the internal-file guard can refuse them.
    for name in (*internal, "brief.md", ".secret"):
        path = root / Path(*name.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic", encoding="utf-8")
    assert run(backend.call("files/path", {"project": "project-one", "name": "brief.md"}))["path"]
    for name in internal:
        with pytest.raises(RpcError) as error:
            run(backend.call("files/path", {"project": "project-one", "name": name}))
        assert cast(dict[str, object], error.value.data)["reason"] == "file_not_found", name
    for name in ("../brief.md", ".secret"):
        with pytest.raises(RpcError) as error:
            run(backend.call("files/path", {"project": "project-one", "name": name}))
        assert cast(dict[str, object], error.value.data)["reason"] == "invalid_name", name
    # On macOS a resolved path keeps the case as typed, so the guard itself must ignore case.
    for name in ("CHAT/History.jsonl", "jobs/job-one/RAW/2.jsonl.gz", "jobs/job-one/Harvested_Names.json"):
        assert ProductionBackend._is_internal_project_file(Path(name)), name


def test_files_path_missing_file_is_file_not_found(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    create_project(backend)
    with pytest.raises(RpcError) as error:
        run(backend.call("files/path", {"project": "project-one", "name": "missing.txt"}))
    assert cast(dict[str, object], error.value.data)["reason"] == "file_not_found"


def codex_scenario(tmp_path: Path, used: int = 20) -> Path:
    (tmp_path / ".bca-test-fakes").touch()
    path = tmp_path / "auth-status" / "fake-codex-scenario.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"used_percent": used}), encoding="utf-8")
    return path


def test_allowance_status_uses_one_cached_codex_read_for_many_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = codex_scenario(tmp_path)
    monkeypatch.setenv("BCA_TEST_FAKES", "1")
    backend = make_backend(tmp_path)
    run(backend.call("allowance/status", {}))
    run(backend.call("allowance/status", {}))
    methods = [
        json.loads(line)
        for line in scenario.with_name(f"{scenario.name}.methods.json")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert methods.count("account/read") == 1
    assert methods.count("account/rateLimits/read") == 1


def test_allowance_status_prefers_a_live_pool_snapshot(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    backend._codex_allowance_cache = (
        backend.clock.now(),
        {"loggedIn": True, "windows": [{"usedPercent": 7}]},
    )
    result = cast(dict[str, object], run(backend.call("allowance/status", {})))
    assert cast(dict[str, object], result["codex"])["windows"] == [{"usedPercent": 7}]


class _AccountWithEmailClient:
    async def request(self, method: str, params: Any = None) -> dict[str, Any]:
        if method == "account/read":
            return {"account": {"type": "chatgpt", "email": "synthetic.person@example.invalid"}}
        return {"rateLimits": {"primary": {"usedPercent": 20, "windowDurationMins": 300, "resetsAt": 1}}}


class _AccountWithEmailServer:
    client = _AccountWithEmailClient()

    async def stop(self) -> None:
        return None


def test_allowance_status_has_no_tokens_or_emails(tmp_path: Path) -> None:
    async def launch() -> _AccountWithEmailServer:
        return _AccountWithEmailServer()

    # A real read through an app-server whose account carries an email address.
    backend = make_backend(tmp_path, launch_pool=launch)
    result = cast(dict[str, Any], run(backend.call("allowance/status", {})))
    assert result["codex"]["loggedIn"] is True
    serialized = json.dumps(result).lower()
    assert "example.invalid" not in serialized
    assert "token" not in serialized and "@" not in serialized and "email" not in serialized


def test_claude_allowance_persists_the_last_rate_limit_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    allow_claude_fake(monkeypatch, tmp_path, "rate_limit_allowed")
    backend = make_backend(tmp_path)
    create_project(backend)
    run(backend.call("chat/send", {"project": "project-one", "text": "hello"}))
    assert (tmp_path / "claude" / "allowance.json").is_file()


def test_login_probes_report_signed_in_and_not_signed_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = make_backend(tmp_path)
    backend._codex_allowance_cache = (backend.clock.now(), {"loggedIn": True, "windows": []})
    monkeypatch.setattr(production, "claude_auth_status", lambda *_: AuthStatus(True, "claude.ai", None))
    signed = cast(dict[str, object], run(backend.call("auth/status", {})))
    assert cast(dict[str, object], signed["claude"])["loggedIn"] is True
    monkeypatch.setattr(production, "claude_auth_status", lambda *_: AuthStatus(False, None, None))
    backend._codex_allowance_cache = (backend.clock.now(), {"loggedIn": False, "windows": []})
    unsigned = cast(dict[str, object], run(backend.call("auth/status", {})))
    assert cast(dict[str, object], unsigned["claude"])["loggedIn"] is False
    assert cast(dict[str, object], unsigned["codex"])["loggedIn"] is False


def test_settings_worker_count_and_retention_persist_across_restart(tmp_path: Path) -> None:
    write_valid_config(tmp_path / "site.toml")
    backend = make_backend(tmp_path)
    run(backend.call("settings/set", {"workerCount": 3, "retentionDays": 12}))
    restarted = make_backend(tmp_path, bq=FakeBqRunner())
    settings = cast(dict[str, object], run(restarted.call("settings/get", {})))
    assert settings["workerCount"] == 3 and settings["retentionDays"] == 12
    # The saved count is applied, not only shown: the engine and the run view use 3 workers.
    assert restarted.concurrency == 3
    _write_job(restarted, "job-one", JobState.full_running)
    assert restarted.runner_deps("job-one", "project-one").concurrency == 3
    snapshot = cast(dict[str, Any], run(restarted.call("job/snapshot", {"job": "job-one"})))
    assert snapshot["activity"]["workers"] == 3


def test_settings_key_path_alias_and_connector_dir(tmp_path: Path) -> None:
    write_valid_config(tmp_path / "site.toml")
    backend = make_backend(tmp_path)
    result = cast(dict[str, object], run(backend.call("settings/get", {})))
    assert result["keyPath"] == result["connectorPath"]
    run(backend.call("settings/set", {"connectorPath": str(tmp_path / "connector.json")}))
    assert cast(dict[str, object], run(backend.call("settings/get", {})))["keyPath"] == str(
        tmp_path / "connector.json"
    )


def test_settings_worker_count_null_means_auto(tmp_path: Path) -> None:
    write_valid_config(tmp_path / "site.toml")
    backend = make_backend(tmp_path)
    settings = cast(dict[str, object], run(backend.call("settings/set", {"workerCount": None})))
    assert settings["workerCountMode"] == "auto"


def test_ids_attach_counts_and_writes_inputs_file(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    create_project(backend)
    result = cast(
        dict[str, object], run(backend.call("ids/attach", {"project": "project-one", "text": "1\n1\n2\nno"}))
    )
    assert result == {"name": "ids-1.txt", "raw": 4, "distinct": 2, "duplicates": 1, "malformed": 1}
    assert (backend._project_dir("project-one") / "inputs" / "ids-1.txt").read_text(
        encoding="utf-8"
    ) == "1\n2\n"


def test_ids_attach_limits_too_large_and_no_ids(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    create_project(backend)
    for text, reason in (("x" * (5 * 1024 * 1024 + 1), "too_large"), ("bad", "no_ids")):
        with pytest.raises(RpcError) as error:
            run(backend.call("ids/attach", {"project": "project-one", "text": text}))
        assert cast(dict[str, object], error.value.data)["reason"] == reason


def _host_for(backend: ProductionBackend) -> ToolHost:
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


def test_propose_job_copies_an_attached_ids_file_into_the_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".bca-test-fakes").touch()
    monkeypatch.setenv("BCA_TEST_FAKES", "1")
    backend = make_backend(
        tmp_path,
        bq=FakeBqRunner(
            preflight_pages=[
                [
                    {
                        "lead_id": "1",
                        "found": True,
                        "calls": 1,
                        "transcript_calls": 1,
                        "usable_calls": 1,
                        "transcription_failed": 0,
                        "transcript_chars": 2,
                    },
                    {
                        "lead_id": "2",
                        "found": True,
                        "calls": 1,
                        "transcript_calls": 1,
                        "usable_calls": 1,
                        "transcription_failed": 0,
                        "transcript_chars": 2,
                    },
                ],
            ],
            dry=DryRun(1, [], []),
        ),
    )
    create_project(backend)
    attached = cast(
        dict[str, object], run(backend.call("ids/attach", {"project": "project-one", "text": "1\n2\n"}))
    )
    spec = JobSpec(
        1,
        "job-ids",
        "project-one",
        "IDs title",
        "question",
        LeadSourceIds(str(attached["name"]), 2),
        usable=UsableRule(min_chars=1),
        cap=CapRule(100, "drop_short_then_oldest", 180),
        instructions="Return JSON.",
        output_schema={"type": "object", "properties": {}},
        luna_model=backend.config.models.luna_model,
        luna_effort=backend.config.models.luna_effort,
        sample_size=1,
    )
    _host_for(backend).call("propose_job", {"spec_json": json.dumps(spec.to_dict())})
    job_dir = backend._project_dir("project-one") / "jobs" / "job-ids"
    record = json.loads((job_dir / "preflight.v1.json").read_text(encoding="utf-8"))
    assert (job_dir / str(attached["name"])).is_file()
    assert record["id_intake"] == {"raw": 2, "distinct": 2, "duplicates": 0, "malformed": 0}
