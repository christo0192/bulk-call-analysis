from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field

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
        kind = self._query_kind(sql)
        if kind in self.pages_by_query_kind:
            pages = self.pages_by_query_kind[kind]
        elif kind == "preflight" and self.preflight_pages is not None:
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
