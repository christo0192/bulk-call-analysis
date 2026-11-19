from __future__ import annotations

from bca.bq.queries import preflight_sql, pull_sql
from bca.config import RecipeConfig
from bca.store.spec import UsableRule


def recipe() -> RecipeConfig:
    return RecipeConfig("transcripts", "calls", "lead_id", "call_id", "text", "started", "duration", "s")


def test_pull_uses_array_parameter_and_not_id_literals() -> None:
    sql, params, _ = pull_sql(recipe(), "all", None)
    assert "@lead_ids" in sql and params == {"lead_ids": []}
    assert "12345" not in sql


def test_preflight_uses_calls_for_found_and_window_duration_and_rules() -> None:
    configured = RecipeConfig(
        "transcripts",
        "calls",
        "lead_id",
        "call_id",
        "text",
        "started",
        "duration",
        "ms",
        transcript_status_column="status",
    )
    sql, _, _ = preflight_sql(
        configured,
        UsableRule(10, ("completed",), 300, (2, 600)),
        "window",
        ("2026-01-01", "2026-01-31"),
    )
    assert "FROM `calls` c LEFT JOIN `transcripts` t" in sql
    assert "DATE(c.`started`)" in sql
    assert "SAFE_DIVIDE(c.`duration`, 1000) >= 300" in sql
    assert "SAFE_DIVIDE(c.`duration`, 1000) >= 180" in sql
    assert "chars IS NOT NULL AND chars > 0" in sql
    assert "transcript_status IN ('completed')" in sql
    assert "COUNTIF(transcript_status IS NOT NULL" in sql


def test_lead_source_wrapper_is_multiline_and_strips_trailing_semicolon() -> None:
    from bca.bq.queries import lead_source_sql

    sql, _, _ = lead_source_sql("SELECT lead_id FROM contacts;  ")
    assert "FROM (\nSELECT lead_id FROM contacts\n) AS source" in sql
