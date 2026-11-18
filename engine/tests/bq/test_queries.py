from __future__ import annotations

from bca.bq.queries import pull_sql
from bca.config import RecipeConfig


def recipe() -> RecipeConfig:
    return RecipeConfig("transcripts", "calls", "lead_id", "call_id", "text", "started", "duration", "s")


def test_pull_uses_array_parameter_and_not_id_literals() -> None:
    sql, params, _ = pull_sql(recipe(), "all", None)
    assert "@lead_ids" in sql and params == {"lead_ids": []}
    assert "12345" not in sql
