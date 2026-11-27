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
    assert "SAFE_DIVIDE(c.`duration`, 1000) AS duration" in sql
    assert "AND duration >= 300" in sql
    # Past the calls CTE the call log's alias is out of scope, so nothing there may name it.
    assert "c.`" not in sql.split("agg_base AS")[1]
    assert "chars IS NOT NULL AND chars > 0" in sql
    assert "transcript_status IN ('completed')" in sql
    assert "COUNTIF(transcript_status IS NOT NULL" in sql


def test_lead_source_wrapper_is_multiline_and_strips_trailing_semicolon() -> None:
    from bca.bq.queries import lead_source_sql

    sql, _, _ = lead_source_sql("SELECT lead_id FROM contacts;  ")
    assert "FROM (\nSELECT lead_id FROM contacts\n) AS source" in sql


def test_each_table_uses_its_own_lead_id_column() -> None:
    # The call log and the transcripts table name the lead id differently.
    configured = RecipeConfig(
        "transcripts",
        "calls",
        "lead_id",
        "call_id",
        "text",
        "started",
        "duration",
        "ms",
        calls_lead_id_column="contact_id",
        transcripts_lead_id_column="crm_lead_id",
    )
    preflight, _, _ = preflight_sql(configured, UsableRule(10, None, None, None), "all", None)
    assert "CAST(c.`contact_id` AS STRING) IN UNNEST(@lead_ids)" in preflight
    assert "SELECT CAST(c.`contact_id` AS STRING) AS lead_id" in preflight
    assert "`lead_id`" not in preflight and "crm_lead_id" not in preflight
    pull, _, _ = pull_sql(configured, "all", None)
    assert "CAST(t.`crm_lead_id` AS STRING) IN UNNEST(@lead_ids)" in pull
    assert "SELECT CAST(t.`crm_lead_id` AS STRING) AS lead_id" in pull
    assert "`lead_id`" not in pull and "contact_id" not in pull


def test_both_tables_fall_back_to_lead_id_column() -> None:
    preflight, _, _ = preflight_sql(recipe(), UsableRule(10, None, None, None), "all", None)
    pull, _, _ = pull_sql(recipe(), "all", None)
    assert "CAST(c.`lead_id` AS STRING) IN UNNEST(@lead_ids)" in preflight
    assert "CAST(t.`lead_id` AS STRING) IN UNNEST(@lead_ids)" in pull


def test_preflight_counts_each_call_once() -> None:
    # A call log that repeats a call, or a call with several transcript rows, gives the join
    # several rows for one call; the count keeps one, the longest transcript then the longest row.
    sql, _, _ = preflight_sql(recipe(), UsableRule(10, None, None, (2, 600)), "all", None)
    calls_cte = sql.split("agg_base AS")[0]
    assert (
        "QUALIFY ROW_NUMBER() OVER (PARTITION BY CAST(c.`lead_id` AS STRING), c.`call_id`\n"
        "ORDER BY chars DESC NULLS LAST, duration DESC NULLS LAST) = 1)"
    ) in calls_cte
    # The window filter still comes first, so a call outside the window is never kept.
    windowed, _, _ = preflight_sql(
        recipe(), UsableRule(10, None, None, None), "window", ("2026-01-01", "2026-01-31")
    )
    assert windowed.index("DATE(c.`started`) BETWEEN") < windowed.index("QUALIFY ROW_NUMBER()")


def test_queries_name_only_columns_in_scope() -> None:
    # Found by a BigQuery dry run on real tables: past each CTE, a query may use only the names
    # that CTE gives, never the source tables' aliases or raw columns.
    configured = RecipeConfig(
        "transcripts",
        "calls",
        "lead_id",
        "call_id",
        "text",
        "started",
        "duration",
        "ms",
        advisor_column="advisor",
        transcript_status_column="status",
    )
    preflight, _, _ = preflight_sql(configured, UsableRule(10, ("completed",), 180, (1, 600)), "all", None)
    agg = preflight.split("\nagg AS (")[1].split(")\nSELECT")[0]
    for name in ("calls", "transcript_chars", "transcript_calls", "usable_calls", "transcription_failed"):
        assert name in agg, name
    pull, _, _ = pull_sql(configured, "all", None)
    final = pull.split(") = 1)\nSELECT")[1]
    assert "t.transcript_status" in final
    assert "t.`status`" not in final and "t.`text`" not in final
