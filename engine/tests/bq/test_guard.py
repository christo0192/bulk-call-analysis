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
