from __future__ import annotations

from pathlib import Path

import pytest

from bca.claude.stream import Event, RateLimitGuard
from bca.luna.quota import INTERRUPT_ALL, QuotaGuard, parse_rate_limits
from tests.sim.harness import Fault, Scenario, run_scenario


@pytest.mark.parametrize("signal", ["credit", "overage", "spend-control", "claude-status"])
def test_credit_or_overage_signal_pauses_or_blocks(tmp_path: Path, signal: str) -> None:
    if signal == "credit":
        result = run_scenario(
            Scenario("credit-pause", faults=(Fault("credit_signal", dispatch=1),)), tmp_path
        )
        # The harness resumes after the pause, so check the persisted pause reason, not the final state.
        rows = result.store.rows("SELECT kind, data_json FROM job_events WHERE job_id=?", ("scenario",))
        assert any(str(kind) == "state" and '"credit"' in str(data) for kind, data in rows)
    elif signal == "overage":
        decision = RateLimitGuard().inspect(
            Event("rate_limit", {"rate_limit_info": {"status": "allowed", "overageEnabled": True}})
        )
        assert decision.blocked_reason is not None
    elif signal == "spend-control":
        snapshot = parse_rate_limits({"rateLimits": {"spendControlReached": True}}, "codex")
        assert QuotaGuard(95, None).decide(snapshot).kind == INTERRUPT_ALL
    else:
        decision = RateLimitGuard().inspect(
            Event("rate_limit", {"rate_limit_info": {"errorCode": "credits_required"}})
        )
        assert decision.blocked_reason is not None or decision.status is not None
