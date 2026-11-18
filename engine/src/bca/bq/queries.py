from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from bca.config import RecipeConfig
from bca.store.spec import UsableRule


@dataclass(frozen=True)
class QueryPlan:
    kind: Literal["lead_source", "count", "preflight", "pull"]
    lead_ids: tuple[str, ...] = ()


def _window(scope: str, window: tuple[str, str] | None, column: str) -> str:
    if scope == "all":
        return ""
    if window is None:
        raise ValueError("window scope requires a date window")
    return f" AND DATE({column}) BETWEEN @window_start AND @window_end"


def lead_source_sql(user_sql: str) -> tuple[str, dict[str, object], QueryPlan]:
    return (
        f"SELECT CAST(lead_id AS STRING) AS lead_id FROM ({user_sql}) AS source",
        {},
        QueryPlan("lead_source"),
    )


def count_sql() -> tuple[str, dict[str, object], QueryPlan]:
    return (
        "SELECT COUNT(*) AS total, COUNT(DISTINCT lead_id) AS distinct_ids FROM source",
        {},
        QueryPlan("count"),
    )


def preflight_sql(
    recipe: RecipeConfig, usable_rule: UsableRule, scope: str, window: tuple[str, str] | None
) -> tuple[str, dict[str, object], QueryPlan]:
    status = (
        f", t.`{recipe.transcript_status_column}` AS transcript_status"
        if recipe.transcript_status_column
        else ""
    )
    status_count = (
        f", COUNTIF(t.`{recipe.transcript_status_column}` IS NOT NULL "
        f"AND t.`{recipe.transcript_status_column}` != 'completed') AS transcription_failed"
        if recipe.transcript_status_column
        else ", 0 AS transcription_failed"
    )
    sql = f"""WITH selected AS (SELECT lead_id FROM UNNEST(@lead_ids) AS lead_id),
calls AS (SELECT CAST(t.`{recipe.lead_id_column}` AS STRING) AS lead_id,
LENGTH(t.`{recipe.transcript_text_column}`) AS chars{status}
FROM `{recipe.transcripts_table}` t WHERE CAST(t.`{recipe.lead_id_column}` AS STRING) IN UNNEST(@lead_ids)
{_window(scope, window, f"t.`{recipe.call_start_column}`")}),
agg AS (SELECT lead_id, COUNT(*) AS calls,
COUNTIF(chars >= {usable_rule.min_chars}) AS usable_calls{status_count}
FROM calls GROUP BY lead_id)
SELECT selected.lead_id, COALESCE(agg.calls, 0) AS calls, COALESCE(agg.usable_calls, 0) AS usable_calls,
COALESCE(agg.transcription_failed, 0) AS transcription_failed,
COALESCE(agg.calls - agg.usable_calls, 0) AS too_short, agg.lead_id IS NOT NULL AS found
FROM selected LEFT JOIN agg USING (lead_id)"""
    params: dict[str, object] = {"lead_ids": []}
    if window is not None:
        params.update({"window_start": window[0], "window_end": window[1]})
    return sql, params, QueryPlan("preflight")


def pull_sql(
    recipe: RecipeConfig, scope: str, window: tuple[str, str] | None
) -> tuple[str, dict[str, object], QueryPlan]:
    advisor = f", c.`{recipe.advisor_column}` AS advisor" if recipe.advisor_column else ""
    status = (
        f", t.`{recipe.transcript_status_column}` AS transcript_status"
        if recipe.transcript_status_column
        else ""
    )
    sql = f"""WITH transcripts AS (SELECT CAST(t.`{recipe.lead_id_column}` AS STRING) AS lead_id,
t.`{recipe.call_id_column}` AS call_id, t.`{recipe.transcript_text_column}` AS transcript_text{status}
FROM `{recipe.transcripts_table}` t WHERE CAST(t.`{recipe.lead_id_column}` AS STRING) IN UNNEST(@lead_ids)
AND LENGTH(t.`{recipe.transcript_text_column}`) > 0{_window(scope, window, f"t.`{recipe.call_start_column}`")}
QUALIFY ROW_NUMBER() OVER (PARTITION BY t.`{recipe.call_id_column}`
ORDER BY LENGTH(t.`{recipe.transcript_text_column}`) DESC) = 1)
SELECT t.lead_id, t.call_id, t.transcript_text, c.`{recipe.call_start_column}` AS call_start,
c.`{recipe.call_duration_column}` AS duration{advisor}{status}
FROM transcripts t LEFT JOIN `{recipe.calls_table}` c ON c.`{recipe.call_id_column}` = t.call_id
QUALIFY ROW_NUMBER() OVER (PARTITION BY t.call_id ORDER BY c.`{recipe.call_duration_column}` DESC) = 1
ORDER BY t.lead_id, call_start ASC, t.call_id"""
    params: dict[str, object] = {"lead_ids": []}
    if window is not None:
        params.update({"window_start": window[0], "window_end": window[1]})
    return sql, params, QueryPlan("pull")
