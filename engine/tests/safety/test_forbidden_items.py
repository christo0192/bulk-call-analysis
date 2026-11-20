from __future__ import annotations

from pathlib import Path

import pytest

from tests.sim.harness import Fault, Population, Scenario, run_scenario


@pytest.mark.parametrize("item_type", ["commandExecution", "fileChange"])
def test_forbidden_item_is_final_for_one_lead(tmp_path: Path, item_type: str) -> None:
    result = run_scenario(
        Scenario(
            "forbidden-item",
            Population(normal=3),
            concurrency=2,
            faults=(Fault("forbidden_item", lead="0", value=item_type),),
        ),
        tmp_path,
    )
    failed = result.store.rows(
        "SELECT lead_id, outcome, COUNT(*) FROM attempts WHERE job_id=? GROUP BY lead_id, outcome",
        ("scenario",),
    )
    assert [
        tuple(row)
        for row in result.store.rows(
            "SELECT error_code, COUNT(*) FROM attempts WHERE job_id=? AND lead_id='0' GROUP BY error_code",
            ("scenario",),
        )
    ] == [("tool_call_forbidden", 1)]
    assert [
        tuple(row)
        for row in result.store.rows("SELECT status FROM leads WHERE job_id=? AND lead_id='0'", ("scenario",))
    ] == [("failed",)]
    assert [tuple(row) for row in failed] == [
        ("0", "failed", 1),
        ("1", "ok", 1),
        ("2", "ok", 1),
    ]
    assert (
        result.store.rows("SELECT COUNT(*) FROM leads WHERE job_id=? AND status='done'", ("scenario",))[0][0]
        >= 2
    )
    result.store.close()
