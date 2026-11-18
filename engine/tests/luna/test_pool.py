import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from bca.luna.appserver import CodexLaunch, start
from bca.luna.pool import LeadTask, LunaPool
from bca.luna.quota import QuotaGuard


class Callbacks:
    def __init__(self, tasks: list[LeadTask]) -> None:
        self.tasks = tasks
        self.results: list[tuple[str, str]] = []
        self.failures: list[tuple[str, str, bool]] = []
        self.pauses: list[tuple[str, int | None]] = []
        self.reroutes: list[tuple[str, str]] = []
        self.quota: list[str] = []
        self.started: list[str] = []
        self.activities = 0

    async def next_tasks(self, n: int) -> list[LeadTask]:
        batch, self.tasks = self.tasks[:n], self.tasks[n:]
        return batch

    async def on_started(self, task: LeadTask) -> None:
        self.started.append(task.attempt_id)

    async def on_result(
        self, task: LeadTask, answer: dict[str, Any], model_used: str, usage: dict[str, Any] | None
    ) -> None:
        self.results.append((task.attempt_id, model_used))

    async def on_failure(self, task: LeadTask, error_code: str, counted: bool, detail: str) -> None:
        self.failures.append((task.attempt_id, error_code, counted))

    async def on_activity(self, ts: float) -> None:
        self.activities += 1

    async def on_quota(self, snapshot: Any, decision: Any) -> None:
        self.quota.append(decision.kind)

    async def on_reroute(self, task: LeadTask, from_model: str, to_model: str, reason: str) -> None:
        self.reroutes.append((task.attempt_id, to_model))

    async def on_pause(self, reason: str, resume_at: int | None) -> None:
        self.pauses.append((reason, resume_at))


def task(index: int, model: str = "codex") -> LeadTask:
    return LeadTask(
        str(index),
        f"attempt-{index}",
        f"prompt {index}",
        {"type": "object", "required": ["ok"]},
        model,
        "low",
    )


async def run_pool(
    tmp_path: Path, scenario: dict[str, object], tasks: list[LeadTask], **kwargs: object
) -> tuple[Callbacks, list[Path]]:
    scenario_path = tmp_path / "scenario.json"
    scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
    launches: list[Path] = []

    async def launch():
        launches.append(scenario_path)
        return await start(
            CodexLaunch(
                (sys.executable, "-m", "tests.fakes.fake_codex", "--scenario", str(scenario_path)),
                tmp_path / "home",
                tmp_path,
            )
        )

    callbacks = Callbacks(tasks)
    pool = LunaPool(
        launch,
        callbacks,
        QuotaGuard(95, Decimal("5")),
        lead_timeout_s=0.05,
        interrupt_grace_s=0.02,
        **kwargs,
    )
    await pool.run_until_idle()
    return callbacks, launches


def test_fifty_leads_exactly_once_and_unsubscribed(tmp_path: Path) -> None:
    import asyncio

    asyncio.run(_test_fifty_leads_exactly_once_and_unsubscribed(tmp_path))


async def _test_fifty_leads_exactly_once_and_unsubscribed(tmp_path: Path) -> None:
    callbacks, launches = await run_pool(
        tmp_path, {"models": ["codex"], "latency_ms": 1}, [task(i) for i in range(50)], concurrency=4
    )
    assert len(callbacks.results) == 50
    assert len({lead for lead, _ in callbacks.results}) == 50
    methods = json.loads(Path(str(launches[0]) + ".methods.json").read_text(encoding="utf-8"))
    assert "account/rateLimitResetCredit/consume" not in methods
    assert all("thread/unsubscribe" in methods for _ in [0])


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ({"answers": {"0": "not json"}}, "invalid_output"),
        ({"answers": {"0": {"wrong": 1}}}, "invalid_output"),
        ({"tool_call": "0"}, "tool_call_forbidden"),
    ],
)
def test_invalid_and_forbidden_outputs_are_counted(
    tmp_path: Path, scenario: dict[str, object], expected: str
) -> None:
    import asyncio

    asyncio.run(_test_invalid_and_forbidden_outputs_are_counted(tmp_path, scenario, expected))


async def _test_invalid_and_forbidden_outputs_are_counted(
    tmp_path: Path, scenario: dict[str, object], expected: str
) -> None:
    callbacks, _ = await run_pool(tmp_path, scenario, [task(0)])
    assert callbacks.failures == [("attempt-0", expected, True)]


def test_reroute_records_new_model(tmp_path: Path) -> None:
    import asyncio

    asyncio.run(_test_reroute_records_new_model(tmp_path))


async def _test_reroute_records_new_model(tmp_path: Path) -> None:
    callbacks, _ = await run_pool(tmp_path, {"reroute": "0", "models": ["codex", "codex-mini"]}, [task(0)])
    assert callbacks.results == [("attempt-0", "codex-mini")]
    assert callbacks.reroutes == [("attempt-0", "codex-mini")]


def test_quota_stop_finishes_inflight_and_pauses(tmp_path: Path) -> None:
    import asyncio

    asyncio.run(_test_quota_stop_finishes_inflight_and_pauses(tmp_path))


async def _test_quota_stop_finishes_inflight_and_pauses(tmp_path: Path) -> None:
    callbacks, _ = await run_pool(
        tmp_path,
        {"used_percent": 94, "quota_increment": 1, "models": ["codex"]},
        [task(i) for i in range(3)],
        concurrency=1,
    )
    assert len(callbacks.results) == 1
    assert callbacks.pauses == [("quota", 9999999999)]


def test_recycle_between_batches_does_not_duplicate(tmp_path: Path) -> None:
    import asyncio

    asyncio.run(_test_recycle_between_batches_does_not_duplicate(tmp_path))


async def _test_recycle_between_batches_does_not_duplicate(tmp_path: Path) -> None:
    callbacks, launches = await run_pool(
        tmp_path, {"models": ["codex"]}, [task(i) for i in range(11)], concurrency=2, recycle_every=10
    )
    assert sorted(lead for lead, _ in callbacks.results) == [f"attempt-{i}" for i in range(11)]
    assert len(launches) == 2


def test_hang_interrupts_and_times_out(tmp_path: Path) -> None:
    import asyncio

    callbacks, _ = asyncio.run(run_pool(tmp_path, {"hang": "0"}, [task(0)]))
    assert callbacks.failures == [("attempt-0", "timeout", True)]


def test_credit_drop_interrupts_inflight_and_pauses(tmp_path: Path) -> None:
    import asyncio

    callbacks, _ = asyncio.run(run_pool(tmp_path, {"credit_drop": True}, [task(0)]))
    assert callbacks.failures == [("attempt-0", "quota_interrupt", False)]
    assert callbacks.pauses[0][0] == "credit"


@pytest.mark.parametrize(
    ("code", "counted"),
    [
        ("rateLimitExceeded", False),
        ("serverOverloaded", False),
        ("httpConnectionFailed", False),
        ("responseStreamDisconnected", False),
        ("responseTooManyFailedAttempts", False),
        ("usageLimitExceeded", False),
        ("unauthorized", False),
        ("modelNotFound", False),
        ("contextWindowExceeded", True),
        ("badRequest", True),
        ("unknown", True),
    ],
)
def test_error_table_maps_actions(tmp_path: Path, code: str, counted: bool) -> None:
    import asyncio

    callbacks, _ = asyncio.run(run_pool(tmp_path, {"errors": {"0": code}}, [task(0)]))
    if code == "unauthorized":
        assert callbacks.pauses == [("auth", None)]
    elif code == "modelNotFound":
        assert callbacks.pauses == [("model_unavailable", None)]
    elif code == "usageLimitExceeded":
        assert callbacks.pauses == [("quota", None)]
    else:
        assert callbacks.failures[0][1:] == (code, counted)


def test_crash_restarts(tmp_path: Path) -> None:
    import asyncio

    marker = tmp_path / "crashed"
    callbacks, launches = asyncio.run(
        run_pool(
            tmp_path,
            {"crash_after_turns": 1, "crash_once_file": str(marker)},
            [task(i) for i in range(2)],
            concurrency=1,
        )
    )
    assert len(launches) >= 2
    assert callbacks.failures[0][1] == "appserver_crash"
