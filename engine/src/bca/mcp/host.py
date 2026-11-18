from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol, cast

from bca.bq.guard import validate_select
from bca.bq.leads import resolve_lead_source
from bca.bq.pull import run_preflight
from bca.bq.runner import BqRunner
from bca.config import BigQueryConfig, RecipeConfig
from bca.mask.masker import MaskingContext, mask_text
from bca.outputs.aggregates import aggregates
from bca.outputs.rules import validate_rules
from bca.store import JobSpec, Store, load_latest_spec, save_spec

MAX_RESULT_CHARS = 40_000
_LOG = logging.getLogger("bca.mcp")
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_VIEWS = {"TABLES", "COLUMNS", "VIEWS", "TABLE_OPTIONS", "COLUMN_FIELD_PATHS", "PARTITIONS"}


class ToolHostError(RuntimeError):
    """A short, safe error intended for an MCP client."""


class ListableRunner(Protocol):
    def list_datasets(self) -> list[dict[str, object]]: ...
    def list_tables(self, dataset: str) -> list[dict[str, object]]: ...
    def describe_table(self, dataset: str, table: str) -> dict[str, object]: ...


def _masked(value: object, context: MaskingContext) -> object:
    if isinstance(value, str):
        return mask_text(value, context).text
    if isinstance(value, list):
        return [_masked(item, context) for item in value]
    if isinstance(value, tuple):
        return [_masked(item, context) for item in value]
    if isinstance(value, dict):
        return {str(key): _masked(item, context) for key, item in value.items()}
    return value


def _cap(value: object, context: MaskingContext, *, unit: str = "characters") -> object:
    masked = _masked(value, context)
    if isinstance(masked, dict) and isinstance(masked.get("rows"), list):
        rows = cast(list[object], masked["rows"])
        prefix = {key: item for key, item in masked.items() if key != "rows"}
        row_kept: list[object] = []
        for item in rows:
            candidate = json.dumps(prefix | {"rows": [*row_kept, item]}, ensure_ascii=False, default=str)
            if len(candidate) > MAX_RESULT_CHARS:
                break
            row_kept.append(item)
        if len(row_kept) < len(rows):
            return prefix | {
                "rows": row_kept,
                "truncated": f"{len(rows) - len(row_kept)} rows omitted; aggregate in SQL or page the result",
            }
    encoded = json.dumps(masked, ensure_ascii=False, default=str)
    if len(encoded) <= MAX_RESULT_CHARS:
        return masked
    if isinstance(masked, list):
        kept: list[object] = []
        for item in masked:
            candidate = json.dumps([*kept, item], ensure_ascii=False, default=str)
            if len(candidate) > MAX_RESULT_CHARS:
                break
            kept.append(item)
        omitted = len(masked) - len(kept)
        return {"items": kept, "truncated": f"{omitted} rows omitted; aggregate in SQL or page the result"}
    omitted = len(encoded) - MAX_RESULT_CHARS
    return {
        "value": encoded[:MAX_RESULT_CHARS],
        "truncated": f"{omitted} {unit} omitted; aggregate in SQL or page the result",
    }


def _safe_error(exc: BaseException) -> ToolHostError:
    if isinstance(exc, ToolHostError):
        return exc
    return ToolHostError(str(exc).splitlines()[0][:300] or "tool failed")


class ToolHost:
    """Engine-side implementations for the two public MCP servers."""

    def __init__(
        self,
        *,
        runner: BqRunner,
        store: Store,
        project_dir: Path,
        bq_config: BigQueryConfig,
        mask_context: MaskingContext | None = None,
        recipe: RecipeConfig | None = None,
    ) -> None:
        self.runner = runner
        self.store = store
        self.project_dir = project_dir
        self.bq_config = bq_config
        self.mask_context = mask_context or MaskingContext()
        self.recipe = recipe

    def call(self, name: str, arguments: Mapping[str, object] | None = None) -> object:
        args = {} if arguments is None else dict(arguments)
        methods: dict[str, Callable[..., object]] = {
            "bq_list_datasets": self.bq_list_datasets,
            "bq_list_tables": self.bq_list_tables,
            "bq_describe_table": self.bq_describe_table,
            "bq_dry_run": self.bq_dry_run,
            "bq_query": self.bq_query,
            "bq_information_schema": self.bq_information_schema,
            "data_notes": self.data_notes,
            "brief_read": self.brief_read,
            "brief_update": self.brief_update,
            "propose_job": self.propose_job,
            "job_status": self.job_status,
            "sample_results": self.sample_results,
            "lead_transcript": self.lead_transcript,
            "result_aggregates": self.result_aggregates,
            "write_file": self.write_file,
        }
        method = methods.get(name)
        if method is None:
            raise ToolHostError("unknown tool")
        try:
            return _cap(method(**args), self.mask_context)
        except BaseException as exc:
            _LOG.warning("MCP tool %s failed: %s", name, _safe_error(exc))
            raise _safe_error(exc) from None

    def _listable(self) -> ListableRunner:
        if not all(hasattr(self.runner, name) for name in ("list_datasets", "list_tables", "describe_table")):
            raise ToolHostError("BigQuery metadata is unavailable")
        return cast(ListableRunner, self.runner)

    def bq_list_datasets(self) -> object:
        return self._listable().list_datasets()

    def bq_list_tables(self, dataset: str) -> object:
        if not _IDENTIFIER.fullmatch(dataset):
            raise ToolHostError("invalid dataset identifier")
        return self._listable().list_tables(dataset)

    def bq_describe_table(self, dataset: str, table: str) -> object:
        if not _IDENTIFIER.fullmatch(dataset) or not _IDENTIFIER.fullmatch(table):
            raise ToolHostError("invalid table identifier")
        return self._listable().describe_table(dataset, table)

    def bq_dry_run(self, sql: str) -> dict[str, object]:
        validate_select(sql)
        dry = self.runner.dry_run(sql)
        return {"bytes": dry.bytes, "referenced_tables": dry.referenced_tables, "schema": dry.schema}

    def bq_query(self, sql: str, max_rows: int = 200) -> object:
        if max_rows < 1 or max_rows > 1000:
            raise ToolHostError("max_rows must be between 1 and 1000")
        validate_select(sql)
        dry = self.runner.dry_run(sql)
        if dry.bytes > self.bq_config.reject_bytes:
            raise ToolHostError(
                "REFUSED: too large for chat; propose it as the job's lead source "
                "so the operator can approve the scan"
            )
        rows: list[dict[str, object]] = []
        for page in self.runner.iter_pages(sql, page_size=min(max_rows, 1000)):
            rows.extend(page[: max_rows - len(rows)])
            if len(rows) >= max_rows:
                break
        return {"bytes": dry.bytes, "rows": rows, "truncated": len(rows) == max_rows}

    def bq_information_schema(self, dataset: str, view: str) -> object:
        if not _IDENTIFIER.fullmatch(dataset) or view not in _VIEWS:
            raise ToolHostError("invalid information schema dataset or view")
        return self.bq_query(
            f"SELECT * FROM `{self.bq_config.project}.{dataset}.INFORMATION_SCHEMA.{view}`", 200
        )

    def _project_file(self, name: str) -> Path:
        return self.project_dir / name

    def data_notes(self) -> str:
        path = self._project_file("notes.md")
        return path.read_text(encoding="utf-8") if path.is_file() else "no notes yet"

    def brief_read(self) -> str:
        path = self._project_file("brief.md")
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def brief_update(self, text: str) -> str:
        if len(text) > 20_000:
            raise ToolHostError("brief is limited to 20k characters")
        self._project_file("brief.md").write_text(text, encoding="utf-8", newline="\n")
        return "updated"

    def propose_job(self, spec_json: str) -> object:
        raw = json.loads(spec_json)
        spec = JobSpec.from_dict(raw)
        validate_rules(spec.rules, spec.output_schema)
        job_dir = self.project_dir / "jobs" / spec.job_id
        path = save_spec(spec, job_dir)
        self.store.create_job(spec.job_id, spec.project_id, spec.spec_version, "plan_proposed")
        intake = resolve_lead_source(
            spec.lead_source, self.runner, self.store, job_dir, self.bq_config, spec.job_id
        )
        summary: dict[str, object] = {
            "job_id": spec.job_id,
            "spec": path.name,
            "state": "plan_proposed",
            "lead_counts": {
                "raw": intake.raw_count,
                "distinct": len(intake.ids),
                "duplicates": intake.duplicates,
                "malformed": len(intake.malformed),
            },
            "rules": [r.to_dict() for r in spec.rules],
        }
        if self.recipe is not None:
            window = (spec.window_start, spec.window_end) if spec.calls_scope == "window" else None
            preflight = run_preflight(
                self.recipe,
                spec.usable,
                spec.calls_scope,
                cast(tuple[str, str] | None, window),
                intake.ids,
                self.runner,
                self.store,
                spec.job_id,
                self.bq_config,
            )
            summary["preflight"] = {"buckets": preflight.buckets, "dry_run_bytes": preflight.dry_run_bytes}
        return summary

    def job_status(self, job_id: str) -> object:
        rows = self.store.rows("SELECT state, spec_version FROM jobs WHERE id=?", (job_id,))
        if not rows:
            raise ToolHostError("job not found")
        counts = self.store.rows(
            "SELECT status, COUNT(*) AS count FROM leads WHERE job_id=? GROUP BY status", (job_id,)
        )
        errors = self.store.rows(
            "SELECT kind, data_json FROM job_events WHERE job_id=? ORDER BY id DESC LIMIT 10", (job_id,)
        )
        return {
            "job_id": job_id,
            "state": str(rows[0][0]),
            "spec_version": rows[0][1],
            "counts": {str(r[0]): r[1] for r in counts},
            "recent_errors": [dict(r) for r in errors],
        }

    def sample_results(self, job_id: str, offset: int = 0, limit: int = 20) -> object:
        if offset < 0 or limit < 1 or limit > 100:
            raise ToolHostError("invalid sample page")
        rows = self.store.rows(
            "SELECT r.lead_id, r.answer_json, r.quotes_ok, r.flags_json "
            "FROM results r JOIN leads l USING(job_id, lead_id) "
            "WHERE r.job_id=? AND l.in_sample=1 ORDER BY r.lead_id LIMIT ? OFFSET ?",
            (job_id, limit, offset),
        )
        return [dict(row) for row in rows]

    def lead_transcript(self, job_id: str, lead_id: str, call_from: int = 1, call_to: int = 3) -> str:
        if call_from < 1 or call_to < call_from:
            raise ToolHostError("invalid call range")
        row = self.store.rows("SELECT in_sample FROM leads WHERE job_id=? AND lead_id=?", (job_id, lead_id))
        if not row or not bool(row[0][0]):
            raise ToolHostError("lead is not a sample lead")
        path = self.project_dir / "jobs" / job_id / "masked" / f"{lead_id}.txt"
        if not path.is_file():
            raise ToolHostError("masked transcript not found")
        calls = path.read_text(encoding="utf-8").split("=== CALL ")
        return "=== CALL ".join(calls[call_from : call_to + 1])

    def result_aggregates(self, job_id: str, group_by: list[str] | None = None) -> object:
        spec, _ = load_latest_spec(self.project_dir / "jobs" / job_id)
        return aggregates(self.store, job_id, spec, group_by)

    def write_file(self, name: str, text: str) -> str:
        if name not in {"plan.md", "summary.md"} or Path(name).name != name:
            raise ToolHostError("only plan.md and summary.md may be written")
        if len(text) > 200_000:
            raise ToolHostError("file is limited to 200k characters")
        self._project_file(name).write_text(text, encoding="utf-8", newline="\n")
        return "written"


def serve_ipc(host: ToolHost, address: str, authkey: bytes) -> None:
    from multiprocessing.connection import Listener

    listener = Listener(address, authkey=authkey)
    try:
        while True:
            connection = listener.accept()
            try:
                request = connection.recv()
                if not isinstance(request, dict):
                    raise ToolHostError("invalid IPC request")
                result = host.call(
                    str(request.get("tool", "")), cast(Mapping[str, object], request.get("arguments", {}))
                )
                connection.send({"ok": True, "result": result})
            except BaseException as exc:
                connection.send({"ok": False, "error": str(_safe_error(exc))})
            finally:
                connection.close()
    finally:
        listener.close()


def start_ipc_thread(host: ToolHost, address: str, authkey: bytes) -> threading.Thread:
    thread = threading.Thread(target=serve_ipc, args=(host, address, authkey), daemon=True)
    thread.start()
    return thread
