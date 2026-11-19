from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from google.cloud import bigquery
from google.oauth2 import service_account

from bca.config import BigQueryConfig

from .guard import ReadOnlyViolation, assert_job_config_read_only, validate_select


@dataclass(frozen=True)
class DryRun:
    bytes: int
    referenced_tables: list[str]
    schema: list[tuple[str, str]]
    statement_type: str = "SELECT"


class BqRunner(Protocol):
    def dry_run(self, sql: str, params: Mapping[str, object] | None = None) -> DryRun: ...
    def iter_pages(
        self,
        sql: str,
        params: Mapping[str, object] | None = None,
        page_size: int = 1000,
        timeout: float | None = None,
    ) -> Iterator[list[dict[str, object]]]: ...

    def list_datasets(self) -> list[dict[str, object]]: ...
    def list_tables(self, dataset: str) -> list[dict[str, object]]: ...
    def describe_table(self, dataset: str, table: str) -> dict[str, object]: ...


class GoogleBqRunner:
    def __init__(self, cfg: BigQueryConfig) -> None:
        if cfg.key_path is None:
            raise ValueError("bigquery.key_path is required")
        credential_factory: Any = service_account.Credentials.from_service_account_file
        credentials = credential_factory(str(Path(cfg.key_path)))
        self._client = bigquery.Client(project=cfg.project, location=cfg.location, credentials=credentials)
        self._cfg = cfg

    def _config(
        self, params: Mapping[str, object] | None, *, dry_run: bool = False, timeout: float | None = None
    ) -> bigquery.QueryJobConfig:
        values = [] if params is None else list(params.items())
        query_parameters: list[Any] = []
        for name, value in values:
            if isinstance(value, (list, tuple)):
                query_parameters.append(bigquery.ArrayQueryParameter(name, "STRING", [str(x) for x in value]))
            elif isinstance(value, str):
                query_parameters.append(bigquery.ScalarQueryParameter(name, "STRING", value))
            else:
                query_parameters.append(bigquery.ScalarQueryParameter(name, "DATE", cast(Any, value)))
        config = bigquery.QueryJobConfig(
            query_parameters=query_parameters,
            use_legacy_sql=False,
            maximum_bytes_billed=self._cfg.max_bytes_billed,
            labels={"app": "bulk-call-analysis"},
            use_query_cache=True,
            dry_run=dry_run,
            job_timeout_ms=max(1, int(timeout * 1000)) if timeout is not None else None,
        )
        assert_job_config_read_only(config)
        return config

    def dry_run(self, sql: str, params: Mapping[str, object] | None = None) -> DryRun:
        validate_select(sql)
        job = self._client.query(sql, job_config=self._config(params, dry_run=True))
        statement_type = str(getattr(job, "statement_type", ""))
        if statement_type != "SELECT":
            raise ReadOnlyViolation("BigQuery did not classify the query as SELECT")
        schema = [(field.name, field.field_type) for field in (job.schema or [])]
        tables = [str(item.table_id) for item in job.referenced_tables or []]
        return DryRun(int(job.total_bytes_processed or 0), tables, schema, statement_type)

    def iter_pages(
        self,
        sql: str,
        params: Mapping[str, object] | None = None,
        page_size: int = 1000,
        timeout: float | None = None,
    ) -> Iterator[list[dict[str, object]]]:
        # Keep the check here: callers cannot execute a query without a fresh
        # dry-run of the exact text that will be sent to BigQuery.
        self.dry_run(sql, params)
        job = self._client.query(sql, job_config=self._config(params, timeout=timeout))
        completed = False
        try:
            result = job.result(page_size=page_size, timeout=timeout)
            page: list[dict[str, object]] = []
            for row in result:
                page.append(dict(row.items()))
                if len(page) == page_size:
                    yield page
                    page = []
            if page:
                yield page
            completed = True
        except TimeoutError:
            raise
        finally:
            if not completed:
                with suppress(Exception):
                    job.cancel()

    def list_datasets(self) -> list[dict[str, object]]:
        return [
            {"dataset_id": item.dataset_id, "location": item.location}
            for item in self._client.list_datasets()
        ]

    def list_tables(self, dataset: str) -> list[dict[str, object]]:
        return [
            {"table_id": item.table_id, "dataset_id": item.dataset_id, "table_type": item.table_type}
            for item in self._client.list_tables(dataset)
        ]

    def describe_table(self, dataset: str, table: str) -> dict[str, object]:
        item = self._client.get_table(f"{self._cfg.project}.{dataset}.{table}")
        return {
            "dataset_id": item.dataset_id,
            "table_id": item.table_id,
            "num_rows": item.num_rows,
            "schema": [
                {"name": field.name, "type": field.field_type, "mode": field.mode} for field in item.schema
            ],
        }
