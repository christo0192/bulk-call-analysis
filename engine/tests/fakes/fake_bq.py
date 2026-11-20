from __future__ import annotations

import json
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from bca.bq.guard import validate_select
from bca.bq.runner import DryRun


@dataclass
class FakeBqRunner:
    """Deterministic runner for query orchestration tests; it never contacts BigQuery."""

    pages: list[list[dict[str, object]]] = field(default_factory=list)
    dry: DryRun = field(default_factory=lambda: DryRun(0, [], []))
    pages_by_query_kind: dict[str, list[list[dict[str, object]]]] = field(default_factory=dict)
    preflight_pages: list[list[dict[str, object]]] | None = None
    error: BaseException | None = None
    calls: list[tuple[str, Mapping[str, object] | None, int]] = field(default_factory=list)
    datasets: list[dict[str, object]] = field(default_factory=list)
    tables: dict[str, list[dict[str, object]]] = field(default_factory=dict)
    table_details: dict[tuple[str, str], dict[str, object]] = field(default_factory=dict)
    delay_ms: int = 0

    @classmethod
    def from_file(cls, path: Path) -> FakeBqRunner:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("fake BigQuery data must be an object")
        pages = value.get("pages", [])
        preflight = value.get("preflight_pages")
        if not isinstance(pages, list) or (preflight is not None and not isinstance(preflight, list)):
            raise ValueError("fake BigQuery pages have an invalid shape")
        delay_ms = value.get("delay_ms", 0)
        if isinstance(delay_ms, bool) or not isinstance(delay_ms, int) or delay_ms < 0:
            raise ValueError("fake BigQuery delay_ms must be a non-negative integer")
        return cls(pages=pages, preflight_pages=preflight, delay_ms=delay_ms)

    def dry_run(self, sql: str, params: Mapping[str, object] | None = None) -> DryRun:
        validate_select(sql)
        self.calls.append((sql, params, 0))
        if self.error is not None:
            raise self.error
        return self.dry

    def iter_pages(
        self,
        sql: str,
        params: Mapping[str, object] | None = None,
        page_size: int = 1000,
        timeout: float | None = None,
    ) -> Iterator[list[dict[str, object]]]:
        validate_select(sql)
        self.calls.append((sql, params, page_size))
        if self.error is not None:
            raise self.error
        if self.delay_ms:
            time.sleep(self.delay_ms / 1000)
        kind = self._query_kind(sql)
        if kind in self.pages_by_query_kind:
            pages = self.pages_by_query_kind[kind]
        elif self.preflight_pages is not None and (kind == "preflight" or "WITH selected AS" in sql):
            pages = self.preflight_pages
        else:
            pages = self.pages
        if params is not None and "lead_ids" in params:
            lead_ids = params["lead_ids"]
            if not isinstance(lead_ids, list):
                raise TypeError("fake lead_ids parameter must be a list")
            requested = {str(lead_id) for lead_id in lead_ids}
            for page in pages:
                yield [row for row in page if str(row.get("lead_id")) in requested]
            return
        yield from pages

    @staticmethod
    def _query_kind(sql: str) -> str:
        if "FROM selected LEFT JOIN agg USING (lead_id)" in sql:
            return "preflight"
        if "CAST(lead_id AS STRING) AS lead_id FROM (" in sql:
            return "lead_source"
        if sql.lstrip().startswith("WITH transcripts AS"):
            return "pull"
        return "other"

    def list_datasets(self) -> list[dict[str, object]]:
        return list(self.datasets)

    def list_tables(self, dataset: str) -> list[dict[str, object]]:
        return list(self.tables.get(dataset, []))

    def describe_table(self, dataset: str, table: str) -> dict[str, object]:
        return dict(
            self.table_details.get(
                (dataset, table), {"dataset_id": dataset, "table_id": table, "num_rows": 0, "schema": []}
            )
        )
