from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any


class ReadOnlyViolation(ValueError):
    """Raised when SQL or a job configuration could write data."""


@dataclass(frozen=True)
class SqlToken:
    kind: str
    text: str


def sql_tokens(sql: str) -> list[SqlToken]:
    """Tokenize SQL while removing comments and preserving literal tokens."""
    tokens: list[SqlToken] = []
    index = 0
    length = len(sql)
    while index < length:
        if sql.startswith("--", index) or sql[index] == "#":
            newline = sql.find("\n", index + (2 if sql.startswith("--", index) else 1))
            index = length if newline < 0 else newline + 1
            continue
        if sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            index = length if end < 0 else end + 2
            continue
        if sql[index].isspace():
            index += 1
            continue
        prefix = ""
        quote_index = index
        match = re.match(r"(?i)(rb|br|r|b)(?=['\"])", sql[index:])
        if match:
            prefix = match.group(0)
            quote_index += len(prefix)
        if quote_index < length and sql[quote_index] in "'\"`":
            quote = sql[quote_index]
            delimiter = quote * 3 if sql.startswith(quote * 3, quote_index) else quote
            cursor = quote_index + len(delimiter)
            while cursor < length:
                if sql[cursor] == "\\":
                    cursor += 2
                    continue
                if sql.startswith(delimiter, cursor):
                    cursor += len(delimiter)
                    break
                if delimiter == quote and sql.startswith(quote * 2, cursor):
                    cursor += 2
                    continue
                cursor += 1
            tokens.append(SqlToken("literal", sql[index:cursor]))
            index = cursor
            continue
        if prefix:
            tokens.append(SqlToken("word", prefix))
            index = quote_index
            continue
        if sql[index].isalnum() or sql[index] in "_$":
            end = index + 1
            while end < length and (sql[end].isalnum() or sql[end] in "_$"):
                end += 1
            tokens.append(SqlToken("word", sql[index:end]))
            index = end
            continue
        tokens.append(SqlToken("punctuation", sql[index]))
        index += 1
    return tokens


def validate_select(sql: str) -> None:
    normalized = unicodedata.normalize("NFKC", sql).strip()
    if not normalized:
        raise ReadOnlyViolation("SQL must not be empty")
    # The BigQuery dry-run is the authority for statement type.  This local
    # check only prevents wrapper break-out and obvious multi-statement input.
    tokens = sql_tokens(normalized)
    if any(token.text == ";" for token in tokens):
        raise ReadOnlyViolation("multiple SQL statements are not allowed")
    if not tokens or tokens[0].text.upper() not in {"SELECT", "WITH"}:
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
