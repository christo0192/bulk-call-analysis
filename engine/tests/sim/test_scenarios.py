from __future__ import annotations

import pytest

from bca.store import JobState

from .harness import Fault, Population, Scenario, SimResult, assert_invariants, run_scenario


def _expect_three_holdbacks(result: SimResult) -> None:
    assert result.outcome_counts.get("held_back") == 2
    pii_statuses = {
        str(lead_id): (str(status), str(reason))
        for lead_id, status, reason in result.store.rows(
            "SELECT lead_id, status, reason FROM leads WHERE job_id=? AND lead_id IN ('4', '5')",
            ("scenario",),
        )
    }
    assert pii_statuses["5"][0] == "done"
    assert pii_statuses["4"] == ("held_back", "leak_scan_hold_back")
    reasons = {
        str(reason)
        for (reason,) in result.store.rows(
            "SELECT reason FROM leads WHERE job_id=? AND status='held_back'", ("scenario",)
        )
    }
    # The deliberate masker failure is caught at the sample harvest (harvest_failed) or at masking.
    assert "leak_scan_hold_back" in reasons
    assert reasons & {"masking_failed", "harvest_failed"}


def _expect_mixed_pii_masked(result: SimResult) -> None:
    statuses = {
        str(lead_id): str(status)
        for lead_id, status in result.store.rows(
            "SELECT lead_id, status FROM leads WHERE job_id=? AND lead_id IN ('12', '13')", ("scenario",)
        )
    }
    assert statuses == {"12": "done", "13": "done"}


SCENARIOS = [
    Scenario("small-happy"),
    Scenario("medium-happy", Population(normal=200), concurrency=4),
    Scenario(
        "mixed-populations",
        Population(6, 2, 2, 2, 2, 1),
        expected_state=JobState.done_with_issues,
        expect=_expect_mixed_pii_masked,
    ),
    Scenario("id-list-quality", source_ids=("0", "0", "bad", "999999")),
    Scenario(
        "quota-reset-canary", faults=(Fault("usage_limit", dispatch=3, value=95), Fault("operator_resume"))
    ),
    Scenario("credit-pause", faults=(Fault("credit_signal", dispatch=3), Fault("operator_resume"))),
    Scenario("explicit-reroute", faults=(Fault("reroute", dispatch=2, value="codex-mini"),)),
    Scenario("auth-recovery", faults=(Fault("auth_lost"), Fault("operator_resume"))),
    Scenario("model-recovery", faults=(Fault("model_unavailable"), Fault("operator_resume"))),
    Scenario("error-storm-recovery", faults=(Fault("error_storm"), Fault("operator_resume"))),
    Scenario("operator-pause", faults=(Fault("operator_pause"), Fault("operator_resume"))),
    Scenario("sleep-requeue", faults=(Fault("sleep_gap", dispatch=2, value=5),)),
    Scenario(
        "two-sleep-gaps",
        faults=(Fault("sleep_gap", dispatch=2, value=5), Fault("sleep_gap", dispatch=4, value=5)),
    ),
    Scenario("crash-sample", faults=(Fault("crash", value="sample"),)),
    Scenario("crash-full", faults=(Fault("crash", value="full"),)),
    Scenario("crash-output", faults=(Fault("crash", value="output"),)),
    Scenario("crash-repair", faults=(Fault("crash", value="repair", lead="lead-00005"),)),
    Scenario("crash-after-recover", faults=(Fault("crash", value="recover"),)),
    Scenario(
        "leak-holdbacks",
        Population(normal=4, pii=2, masker_hold_back=1),
        expected_state=JobState.done_with_issues,
        expect=_expect_three_holdbacks,
    ),
    Scenario("quote-repair", faults=(Fault("quote_fail", lead="lead-00000"),)),
    Scenario(
        "quote-repair-fails",
        faults=(Fault("quote_fail", lead="lead-00000"), Fault("schema_invalid", lead="lead-00000")),
        expected_state=JobState.done_with_issues,
    ),
    Scenario(
        "schema-retry-exhausted",
        faults=(Fault("schema_invalid", lead="lead-00000"),),
        expected_state=JobState.done_with_issues,
    ),
    Scenario("transient-retry", faults=(Fault("error", dispatch=2, value="serverOverloaded"),)),
    Scenario(
        "error-storm-pause",
        Population(normal=10),
        faults=(Fault("error_storm"),),
        expected_state=JobState.done,
    ),
    Scenario("rebuild-rules", faults=(Fault("rebuild"),)),
    Scenario("rebuild-twice", faults=(Fault("rebuild"), Fault("rebuild"))),
    Scenario("stale-plan-gate", faults=(Fault("stale_spec", value="plan"),)),
    Scenario("stale-sample-gate", faults=(Fault("stale_spec", value="sample"),)),
    Scenario("stale-full-gate", faults=(Fault("stale_spec", value="full"),)),
    Scenario("automated-plan-gate", faults=(Fault("automated_actor", value="plan"),)),
    Scenario("automated-sample-gate", faults=(Fault("automated_actor", value="sample"),)),
    Scenario("automated-full-gate", faults=(Fault("automated_actor", value="full"),)),
    Scenario("slow-liveness", faults=(Fault("latency", value=20),)),
    # The hung lead times out and fails (counted), so the job ends with issues.
    Scenario("long-stall", concurrency=3, faults=(Fault("stall"),), expected_state=JobState.done_with_issues),
    Scenario("stdio-api", faults=(Fault("stdio_api"),)),
]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda item: item.name)
def test_scenario(scenario: Scenario, tmp_path: object) -> None:
    result = run_scenario(scenario, tmp_path)  # type: ignore[arg-type]
    assert_invariants(result)
    assert result.state is scenario.expected_state
