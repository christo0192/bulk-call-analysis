from __future__ import annotations

from pathlib import Path

import pytest

from .harness import Population, Scenario, assert_invariants, run_scenario


@pytest.mark.slow
@pytest.mark.parametrize("concurrency", [2, 4, 8])
def test_ramp(concurrency: int, tmp_path: Path) -> None:
    result = run_scenario(
        Scenario(f"ramp-{concurrency}", Population(normal=300), concurrency=concurrency), tmp_path
    )
    assert_invariants(result)


@pytest.mark.slow
@pytest.mark.timeout(600)  # 1000 leads at concurrency 8: a soak test, slow by design
def test_soak(tmp_path: Path) -> None:
    result = run_scenario(Scenario("soak", Population(normal=1000), concurrency=8), tmp_path)
    assert_invariants(result)
