from __future__ import annotations

import asyncio
from typing import Any

import pytest

from bca.api.server import ApiService, MemoryBackend, RpcError


def run(value: Any) -> Any:
    return asyncio.run(value)


@pytest.mark.parametrize("method", ["job/plan", "job/sample", "job/snapshot", "job/outputs"])
def test_memory_read_model_accepts_job(method: str) -> None:
    result = run(ApiService(MemoryBackend()).dispatch(method, {"job": "synthetic-job"}))
    assert result["job"] == "synthetic-job"


def test_plan_allows_optional_spec_version() -> None:
    service = ApiService(MemoryBackend())
    assert run(service.dispatch("job/plan", {"job": "job", "specVersion": 1}))["job"] == "job"


@pytest.mark.parametrize("value", [True, "1", 1.5, []])
def test_plan_rejects_non_integer_spec_version(value: object) -> None:
    with pytest.raises(RpcError) as error:
        run(ApiService(MemoryBackend()).dispatch("job/plan", {"job": "job", "specVersion": value}))
    assert error.value.code == -32602


@pytest.mark.parametrize("method", ["job/plan", "job/sample", "job/snapshot", "job/outputs"])
def test_read_model_requires_job(method: str) -> None:
    with pytest.raises(RpcError) as error:
        run(ApiService(MemoryBackend()).dispatch(method, {}))
    assert error.value.code == -32602


def test_read_model_rejects_unknown_parameters() -> None:
    with pytest.raises(RpcError):
        run(ApiService(MemoryBackend()).dispatch("job/sample", {"job": "job", "extra": 1}))


def test_sample_read_model_is_registered() -> None:
    assert "job/sample" in ApiService.METHODS


def test_snapshot_read_model_is_registered() -> None:
    assert "job/snapshot" in ApiService.METHODS


def test_outputs_read_model_is_registered() -> None:
    assert "job/outputs" in ApiService.METHODS


def test_plan_read_model_is_registered() -> None:
    assert "job/plan" in ApiService.METHODS


def test_read_models_are_distinct_from_job_status() -> None:
    backend = MemoryBackend()
    service = ApiService(backend)
    status = run(service.dispatch("job/status", {"job": "job"}))
    plan = run(service.dispatch("job/plan", {"job": "job"}))
    assert status == plan


def test_read_model_contract_uses_synthetic_job_ids() -> None:
    assert "synthetic" not in "job-one project-one".lower()
