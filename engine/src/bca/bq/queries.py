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
    user_sql = user_sql.strip().removesuffix(";").rstrip()
    return (
        f"SELECT CAST(lead_id AS STRING) AS lead_id FROM (\n{user_sql}\n) AS source",
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
        ", COUNTIF(transcript_status IS NOT NULL AND "
        "transcript_status != 'completed') AS transcription_failed"
        if recipe.transcript_status_column
        else ", NULL AS transcription_failed"
    )
    divisor = 1000 if recipe.duration_unit == "ms" else 1
    duration = f"SAFE_DIVIDE(c.`{recipe.call_duration_column}`, {divisor})"
    status_rule = ""
    if usable_rule.statuses is not None:
        if recipe.transcript_status_column is None:
            raise ValueError("usable.statuses requires recipe.transcript_status_column")
        statuses = ", ".join(
            "'" + status.replace("\\", "\\\\").replace("'", "\\'") + "'" for status in usable_rule.statuses
        )
        status_rule = f" AND transcript_status IN ({statuses})"
    duration_rule = (
        f" AND {duration} >= {usable_rule.min_call_seconds}"
        if usable_rule.min_call_seconds is not None
        else ""
    )
    over_rule = ", NULL AS over_calls"
    over_select = "usable_calls AS usable_calls"
    if usable_rule.min_calls_over is not None:
        count, seconds = usable_rule.min_calls_over
        over_rule = f", COUNTIF(duration >= {seconds}) AS over_calls"
        over_select = f"IF(over_calls >= {count}, usable_calls, 0) AS usable_calls"
    sql = f"""WITH selected AS (SELECT lead_id FROM UNNEST(@lead_ids) AS lead_id),
calls AS (SELECT CAST(c.`{recipe.lead_id_column}` AS STRING) AS lead_id,
LENGTH(t.`{recipe.transcript_text_column}`) AS chars{status}, {duration} AS duration,
c.`{recipe.call_id_column}` AS call_id
FROM `{recipe.calls_table}` c LEFT JOIN `{recipe.transcripts_table}` t
ON c.`{recipe.call_id_column}` = t.`{recipe.call_id_column}`
WHERE CAST(c.`{recipe.lead_id_column}` AS STRING) IN UNNEST(@lead_ids)
{_window(scope, window, f"c.`{recipe.call_start_column}`")}),
agg_base AS (SELECT lead_id, COUNT(DISTINCT call_id) AS calls,
SUM(COALESCE(chars, 0)) AS transcript_chars,
COUNTIF(chars IS NOT NULL AND chars > 0) AS transcript_calls,
COUNTIF(chars >= {usable_rule.min_chars}{status_rule}{duration_rule}) AS usable_calls{status_count}{over_rule}
FROM calls GROUP BY lead_id),
agg AS (SELECT lead_id, calls, transcript_calls, {over_select}, transcription_failed FROM agg_base)
SELECT selected.lead_id, COALESCE(agg.calls, 0) AS calls,
COALESCE(agg.transcript_chars, 0) AS transcript_chars,
COALESCE(agg.usable_calls, 0) AS usable_calls,
COALESCE(agg.transcript_calls, 0) AS transcript_calls,
agg.transcription_failed AS transcription_failed,
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
AND LENGTH(t.`{recipe.transcript_text_column}`) > 0
AND EXISTS (SELECT 1 FROM `{recipe.calls_table}` c0
WHERE c0.`{recipe.call_id_column}` = t.`{recipe.call_id_column}`
{_window(scope, window, f"c0.`{recipe.call_start_column}`")})
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
