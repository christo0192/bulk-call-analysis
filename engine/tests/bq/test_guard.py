from __future__ import annotations

import pytest

from bca.bq.guard import ReadOnlyViolation, validate_select


@pytest.mark.parametrize(
    "verb",
    [
        "CREATE",
        "INSERT",
        "UPDATE",
        "DELETE",
        "MERGE",
        "DROP",
        "ALTER",
        "TRUNCATE",
        "GRANT",
        "REVOKE",
        "CALL",
        "EXECUTE IMMEDIATE",
        "LOAD",
        "EXPORT DATA",
        "BEGIN",
        "END",
        "DECLARE",
        "SET",
        "ASSERT",
    ],
)
def test_blocked_verbs(verb: str) -> None:
    with pytest.raises(ReadOnlyViolation):
        validate_select(f"SELECT 1; {verb} thing")


@pytest.mark.parametrize(
    "sql", ["SELECT 1; SELECT 2", "-- SELECT 1\nINSERT INTO x VALUES (1)", "\uff33\uff25\uff34 x = 1"]
)
def test_comments_fullwidth_and_multiple_statements_are_rejected(sql: str) -> None:
    with pytest.raises(ReadOnlyViolation):
        validate_select(sql)


@pytest.mark.parametrize("sql", ["SELECT 1", "WITH x AS (SELECT 1) SELECT * FROM x"])
def test_selects_are_allowed(sql: str) -> None:
    validate_select(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT '''it's -- ''' AS x; DROP TABLE ds.t; SELECT 1",
        "SELECT '\\\\' AS a, '/* ' AS b; DROP TABLE ds.t; SELECT ' */' AS c",
        "SELECT '''a'b -- ''' AS x; EXPORT DATA OPTIONS(uri='gs://b/*.csv', format='CSV') AS SELECT 1",
        "SELECT '''a'b -- ''' AS x; EXECUTE IMMEDIATE 'DROP TABLE ds.t'",
        "BEGIN DROP TABLE ds.t; END",
    ],
)
def test_statement_breakout_is_rejected_even_when_quotes_contain_comments(sql: str) -> None:
    with pytest.raises(ReadOnlyViolation):
        validate_select(sql)


def test_semicolon_inside_a_literal_is_allowed_locally() -> None:
    validate_select("SELECT ';' AS value")


def test_google_runner_requires_bigquery_select_statement_type() -> None:
    from types import SimpleNamespace

    from bca.bq.runner import GoogleBqRunner

    runner = GoogleBqRunner.__new__(GoogleBqRunner)
    runner._cfg = SimpleNamespace(max_bytes_billed=100, project="p")
    runner._client = SimpleNamespace(
        query=lambda sql, job_config: SimpleNamespace(
            statement_type="SCRIPT", schema=[], referenced_tables=[], total_bytes_processed=0
        )
    )
    with pytest.raises(ReadOnlyViolation):
        runner.dry_run("SELECT 1")
