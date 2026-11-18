from __future__ import annotations

import re
import unicodedata
from typing import Any


class ReadOnlyViolation(ValueError):
    """Raised when SQL or a job configuration could write data."""


def validate_select(sql: str) -> None:
    normalized = unicodedata.normalize("NFKC", sql).strip()
    if not normalized:
        raise ReadOnlyViolation("SQL must not be empty")
    # The BigQuery dry-run is the authority for statement type.  This local
    # check only prevents wrapper break-out and obvious multi-statement input.
    if _has_unquoted_semicolon(normalized):
        raise ReadOnlyViolation("multiple SQL statements are not allowed")
    if re.match(r"^(?:SELECT|WITH)\b", normalized, re.IGNORECASE) is None:
        raise ReadOnlyViolation("only SELECT or WITH queries are allowed")


def _has_unquoted_semicolon(sql: str) -> bool:
    index = 0
    quote = ""
    raw = False
    while index < len(sql):
        if quote:
            if sql.startswith(quote, index):
                index += len(quote)
                quote = ""
            elif sql[index] == "\\" and not raw:
                index += 2
                continue
            else:
                index += 1
            continue
        if sql.startswith("--", index):
            newline = sql.find("\n", index + 2)
            index = len(sql) if newline < 0 else newline + 1
            continue
        if sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            if end < 0:
                return False
            index = end + 2
            continue
        if sql[index] in "'\"`":
            delimiter = sql[index] * 3 if sql.startswith(sql[index] * 3, index) else sql[index]
            raw = index > 0 and sql[index - 1] in "rR"
            quote = delimiter
            index += len(delimiter)
            continue
        if sql[index] == ";":
            return True
        index += 1
    return False


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
