from __future__ import annotations

import asyncio
from typing import Any

from bca.store import JobState


def test_bundle_cache_keeps_metadata_not_text(coordinator: Any) -> None:
    coordinator.runner.on_plan_approved(1)
    asyncio.run(asyncio.wait_for(coordinator.runner.run_sample(), timeout=15))

    assert all(value[1] is None for value in coordinator.runner._bundles.values())
    assert coordinator.runner._state() is JobState.sample_ready


def test_phase_stops_when_store_state_changes(coordinator: Any) -> None:
    coordinator.runner.on_plan_approved(1)
    coordinator.store.connection.execute(
        "UPDATE jobs SET state='plan_proposed', resume_state=NULL WHERE id='job'"
    )
    coordinator.store.connection.commit()

    asyncio.run(asyncio.wait_for(coordinator.runner.run_sample(), timeout=15))
    assert coordinator.runner._state() is JobState.plan_proposed
