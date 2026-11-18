from __future__ import annotations

import re
import unicodedata
from typing import Any

import sqlparse


class ReadOnlyViolation(ValueError):
    """Raised when SQL or a job configuration could write data."""


_BLOCKED = re.compile(
    r"\b(?:CREATE|INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|TRUNCATE|GRANT|REVOKE|CALL|EXECUTE\s+IMMEDIATE|LOAD|EXPORT\s+DATA|BEGIN|END|DECLARE|SET|ASSERT)\b",
    re.IGNORECASE,
)


def validate_select(sql: str) -> None:
    normalized = unicodedata.normalize("NFKC", sqlparse.format(sql, strip_comments=True)).strip()
    statements = [item for item in sqlparse.parse(normalized) if item.tokens]
    if len(statements) != 1:
        raise ReadOnlyViolation("exactly one SQL statement is required")
    if _BLOCKED.search(normalized):
        raise ReadOnlyViolation("SQL contains a blocked statement")
    if re.match(r"^(?:SELECT|WITH)\b", normalized, re.IGNORECASE) is None:
        raise ReadOnlyViolation("only SELECT or WITH queries are allowed")


def assert_job_config_read_only(cfg: Any) -> None:
    if getattr(cfg, "destination", None) is not None:
        raise ReadOnlyViolation("destination table is not allowed")
    disposition = getattr(cfg, "write_disposition", None)
    if disposition not in (None, ""):
        raise ReadOnlyViolation("write disposition is not allowed")
    if getattr(cfg, "create_disposition", None) not in (None, ""):
        raise ReadOnlyViolation("DDL/create disposition is not allowed")
    if getattr(cfg, "ddl_target_table", None) is not None:
        raise ReadOnlyViolation("DDL target is not allowed")
