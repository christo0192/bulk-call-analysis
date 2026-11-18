from __future__ import annotations

import json
import logging
import re
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import replace
from multiprocessing import AuthenticationError
from multiprocessing.connection import Client
from pathlib import Path
from typing import Any, Protocol, cast

from bca.bq.guard import validate_select
from bca.bq.leads import resolve_lead_source
from bca.bq.pull import run_preflight
from bca.bq.runner import BqRunner
from bca.config import BigQueryConfig, RecipeConfig
from bca.mask.masker import MaskingContext, _NoopNer, mask_text
from bca.outputs.aggregates import aggregates
from bca.outputs.rules import validate_rules
from bca.store import JobSpec, Store, load_latest_spec, save_spec

MAX_RESULT_CHARS = 40_000
_LOG = logging.getLogger("bca.mcp")
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PATH_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$", re.ASCII)
_VIEWS = {"TABLES", "COLUMNS", "VIEWS", "TABLE_OPTIONS", "COLUMN_FIELD_PATHS", "PARTITIONS"}


def _has_forbidden_reconstruction(sql: str) -> bool:
    words = {
        "TO_CODE_POINTS",
        "CODE_POINTS_TO_STRING",
        "CODE_POINTS_TO_BYTES",
        "TO_HEX",
        "FROM_HEX",
        "ASCII",
        "CHR",
        "UNICODE",
    }
    scrubbed = re.sub(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"", "", sql)
    return any(re.search(rf"\b{re.escape(word)}\s*\(", scrubbed, re.IGNORECASE) for word in words)


class ToolHostError(RuntimeError):
    """A short, safe error intended for an MCP client."""


class ListableRunner(Protocol):
    def list_datasets(self) -> list[dict[str, object]]: ...
    def list_tables(self, dataset: str) -> list[dict[str, object]]: ...
    def describe_table(self, dataset: str, table: str) -> dict[str, object]: ...


def _masked(
    value: object,
    context: MaskingContext,
    *,
    exempt_numbers: frozenset[str] = frozenset(),
    exempt_number_value: bool = False,
) -> object:
    if isinstance(value, str):
        return mask_text(value[:MAX_RESULT_CHARS], context).text
    if isinstance(value, list):
        return [_masked(item, context, exempt_numbers=exempt_numbers) for item in value]
    if isinstance(value, tuple):
        return [_masked(item, context, exempt_numbers=exempt_numbers) for item in value]
    if isinstance(value, dict):
        merged: dict[str, object] = {}
        for key, item in value.items():
            masked_key = str(_masked(str(key), context, exempt_numbers=exempt_numbers))
            masked_value = _masked(
                item,
                context,
                exempt_numbers=exempt_numbers,
                exempt_number_value=str(key) in exempt_numbers,
            )
            if masked_key in merged and isinstance(merged[masked_key], int) and isinstance(masked_value, int):
                merged[masked_key] = cast(int, merged[masked_key]) + masked_value
            else:
                merged[masked_key] = masked_value
        return merged
    if isinstance(value, bytes):
        return f"[BYTES {len(value)}]"
    if isinstance(value, int) and not isinstance(value, bool):
        digits = str(abs(value))
        normalized = digits[2:] if digits.startswith("91") and len(digits) in (12, 13) else digits
        normalized = normalized[1:] if normalized.startswith("0") else normalized
        if 10 <= len(normalized) <= 13 and not exempt_number_value:
            return "[NUMBER]"
        return value
    if value is None or isinstance(value, (float, bool)):
        return value
    return mask_text(str(value)[:MAX_RESULT_CHARS], context).text


def _cap(
    value: object,
    context: MaskingContext,
    *,
    unit: str = "characters",
    exempt_numbers: frozenset[str] = frozenset(),
) -> object:
    masked = _masked(value, context, exempt_numbers=exempt_numbers)
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


def _safe_error(exc: BaseException, context: MaskingContext) -> ToolHostError:
    if isinstance(exc, ToolHostError):
        reason = str(exc)
    elif isinstance(exc, (ValueError, TypeError, KeyError)):
        reason = str(exc).splitlines()[0]
    elif isinstance(exc, OSError):
        reason = "BigQuery request failed"
    else:
        reason = (
            "BigQuery rejected the query" if exc.__class__.__module__.startswith("google.") else "tool failed"
        )
    return ToolHostError(mask_text(reason[:300], context).text or "tool failed")


class ToolHost:
    """Engine-side implementations for the two public MCP servers."""

    def __init__(
        self,
        *,
        runner: BqRunner,
        store: Store,
        project_dir: Path,
        bq_config: BigQueryConfig,
        mask_context: MaskingContext,
        project_id: str = "project",
        recipe: RecipeConfig | None = None,
    ) -> None:
        self.runner = runner
        self.store = store
        self.project_dir = project_dir
        self.bq_config = bq_config
        if isinstance(mask_context.ner, _NoopNer) or mask_context.ner is None:
            raise ValueError("a real masking NER context is required")
        self.mask_context = mask_context
        self.project_id = project_id
        self.recipe = recipe
        self._ipc_stop = threading.Event()
        self._ipc_listener: object | None = None
        self._ipc_thread: threading.Thread | None = None
        self._ipc_address: str | None = None
        self._ipc_family: str | None = None
        self._ipc_authkey: bytes | None = None

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
            return _cap(method(**args), self.mask_context, exempt_numbers=self._number_exemptions())
        except BaseException as exc:
            safe = _safe_error(exc, self.mask_context)
            _LOG.warning("MCP tool %s failed: %s", name, safe)
            raise safe from None

    def _number_exemptions(self) -> frozenset[str]:
        if self.recipe is None:
            return frozenset()
        return frozenset((self.recipe.lead_id_column, self.recipe.call_id_column))

    def _job_dir(self, job_id: str) -> Path:
        if _PATH_ID.fullmatch(job_id) is None:
            raise ToolHostError("invalid job id")
        jobs = (self.project_dir / "jobs").resolve()
        candidate = (jobs / job_id).resolve()
        try:
            candidate.relative_to(jobs)
        except ValueError:
            raise ToolHostError("invalid job id") from None
        return candidate

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
        if _has_forbidden_reconstruction(sql):
            raise ToolHostError("query can reconstruct unmasked text")
        dry = self.runner.dry_run(sql)
        if dry.bytes > self.bq_config.reject_bytes:
            raise ToolHostError(
                "REFUSED: too large for chat; propose it as the job's lead source "
                "so the operator can approve the scan"
            )
        rows: list[dict[str, object]] = []
        omitted_rows = 0
        omitted_chars = 0
        for page in self.runner.iter_pages(sql, page_size=min(max_rows, 1000)):
            for row in page:
                if len(rows) >= max_rows:
                    omitted_rows += 1
                    omitted_chars += len(json.dumps(row, ensure_ascii=False, default=str))
                    break
                raw_size = len(json.dumps(row, ensure_ascii=False, default=str))
                if (
                    raw_size > MAX_RESULT_CHARS
                    or len(json.dumps(rows, ensure_ascii=False, default=str)) + raw_size > MAX_RESULT_CHARS
                ):
                    omitted_rows = 1
                    omitted_chars = raw_size
                    break
                masked = cast(
                    dict[str, object],
                    _masked(row, self.mask_context, exempt_numbers=self._number_exemptions()),
                )
                candidate = json.dumps(
                    {"bytes": dry.bytes, "rows": [*rows, masked]}, ensure_ascii=False, default=str
                )
                if len(candidate) > MAX_RESULT_CHARS:
                    omitted_rows = 1
                    omitted_chars = raw_size
                    break
                rows.append(masked)
            if len(rows) >= max_rows or omitted_rows:
                break
        result: dict[str, object] = {"bytes": dry.bytes, "rows": rows, "truncated": bool(omitted_rows)}
        if omitted_rows:
            result["omitted"] = f"{omitted_rows} rows omitted; {omitted_chars} characters omitted"
        return result

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
        spec = replace(JobSpec.from_dict(raw), project_id=self.project_id)
        validate_rules(spec.rules, spec.output_schema)
        job_dir = self._job_dir(spec.job_id)
        if hasattr(spec.lead_source, "ids_file"):
            ids_file = Path(str(cast(Any, spec.lead_source).ids_file))
            if ids_file.is_absolute() or ids_file.name != str(ids_file) or ".." in ids_file.parts:
                raise ToolHostError("invalid lead source path")
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
        self._job_dir(job_id)
        rows = self.store.rows(
            "SELECT state, spec_version FROM jobs WHERE id=? AND project_id=?", (job_id, self.project_id)
        )
        if not rows:
            raise ToolHostError("job not found")
        counts = self.store.rows(
            "SELECT l.status, COUNT(*) AS count FROM leads l JOIN jobs j ON j.id=l.job_id "
            "WHERE l.job_id=? AND j.project_id=? GROUP BY l.status",
            (job_id, self.project_id),
        )
        errors = self.store.rows(
            "SELECT e.kind, e.data_json FROM job_events e JOIN jobs j ON j.id=e.job_id "
            "WHERE e.job_id=? AND j.project_id=? ORDER BY e.id DESC LIMIT 10",
            (job_id, self.project_id),
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
        self._job_dir(job_id)
        rows = self.store.rows(
            "SELECT r.lead_id, r.answer_json, r.quotes_ok, r.flags_json "
            "FROM results r JOIN leads l USING(job_id, lead_id) JOIN jobs j ON j.id=r.job_id "
            "WHERE r.job_id=? AND j.project_id=? AND l.in_sample=1 ORDER BY r.lead_id LIMIT ? OFFSET ?",
            (job_id, self.project_id, limit, offset),
        )
        return [dict(row) for row in rows]

    def lead_transcript(self, job_id: str, lead_id: str, call_from: int = 1, call_to: int = 3) -> str:
        if call_from < 1 or call_to < call_from:
            raise ToolHostError("invalid call range")
        self._job_dir(job_id)
        if _PATH_ID.fullmatch(lead_id) is None:
            raise ToolHostError("invalid lead id")
        row = self.store.rows(
            "SELECT l.in_sample FROM leads l JOIN jobs j ON j.id=l.job_id "
            "WHERE l.job_id=? AND l.lead_id=? AND j.project_id=?",
            (job_id, lead_id, self.project_id),
        )
        if not row or not bool(row[0][0]):
            raise ToolHostError("lead is not a sample lead")
        path = self._job_dir(job_id) / "masked" / f"{lead_id}.txt"
        if not path.is_file():
            raise ToolHostError("masked transcript not found")
        calls = path.read_text(encoding="utf-8").split("=== CALL ")
        return "=== CALL ".join(calls[call_from : call_to + 1])

    def result_aggregates(self, job_id: str, group_by: list[str] | None = None) -> object:
        self._job_dir(job_id)
        if not self.store.rows("SELECT 1 FROM jobs WHERE id=? AND project_id=?", (job_id, self.project_id)):
            raise ToolHostError("job not found")
        spec, _ = load_latest_spec(self._job_dir(job_id))
        return aggregates(self.store, job_id, spec, group_by)

    def write_file(self, name: str, text: str) -> str:
        if name not in {"plan.md", "summary.md"} or Path(name).name != name:
            raise ToolHostError("only plan.md and summary.md may be written")
        if len(text) > 200_000:
            raise ToolHostError("file is limited to 200k characters")
        self._project_file(name).write_text(text, encoding="utf-8", newline="\n")
        return "written"

    def close(self) -> None:
        """Stop a listener started with :func:`start_ipc_thread`."""
        self._ipc_stop.set()
        address = self._ipc_address
        family = self._ipc_family
        authkey = self._ipc_authkey
        if address is not None and family is not None and authkey is not None:

            def wake_listener() -> None:
                try:
                    wake = Client(address, family=family, authkey=authkey)
                    wake.close()
                except (OSError, EOFError, AuthenticationError):
                    pass

            threading.Thread(target=wake_listener, daemon=True).start()
        thread = self._ipc_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
        listener = self._ipc_listener
        if listener is not None:
            cast(Any, listener).close()
        if address is not None and family is not None:
            _remove_ipc_socket(address, family)
        if thread is not None and thread is not threading.current_thread() and thread.is_alive():
            raise RuntimeError("IPC listener did not stop")


def _remove_ipc_socket(address: str, family: str) -> None:
    if family != "AF_UNIX":
        return
    path = Path(address)
    try:
        path.unlink(missing_ok=True)
        if path.parent.name.startswith("bca-ipc-"):
            path.parent.rmdir()
    except OSError:
        _LOG.warning("could not clean up IPC socket", exc_info=True)


def serve_ipc(
    host: ToolHost,
    address: str,
    authkey: bytes,
    family: str | None = None,
    stop_event: threading.Event | None = None,
) -> None:
    from multiprocessing.connection import Listener

    selected_family = family or ("AF_PIPE" if sys.platform == "win32" else "AF_UNIX")
    stopping = stop_event or host._ipc_stop
    host._ipc_address = address
    host._ipc_family = selected_family
    host._ipc_authkey = authkey
    if host._ipc_thread is None:
        host._ipc_thread = threading.current_thread()
    listener = Listener(address, family=selected_family, authkey=authkey)
    host._ipc_listener = listener
    if selected_family == "AF_UNIX":
        with suppress(OSError):
            Path(address).chmod(0o600)
    accepted: deque[object | BaseException] = deque()
    accepted_lock = threading.Lock()

    def accept_one() -> None:
        try:
            item: object | BaseException = listener.accept()
        except (OSError, EOFError, AuthenticationError) as exc:
            item = exc
        with accepted_lock:
            accepted.append(item)

    def handle(connection: Any) -> None:
        try:
            if stopping.is_set() or not connection.poll(0.5):
                return
            request = connection.recv()
            if not isinstance(request, dict):
                raise ToolHostError("invalid IPC request")
            result = host.call(
                str(request.get("tool", "")), cast(Mapping[str, object], request.get("arguments", {}))
            )
            try:
                connection.send({"ok": True, "result": result})
            except (OSError, EOFError, AuthenticationError) as exc:
                _LOG.warning("MCP IPC send failed: %s", type(exc).__name__)
        except (OSError, EOFError, AuthenticationError) as exc:
            _LOG.warning("MCP IPC connection failed: %s", type(exc).__name__)
        except BaseException as exc:
            try:
                connection.send({"ok": False, "error": str(_safe_error(exc, host.mask_context))})
            except (OSError, EOFError, AuthenticationError) as send_exc:
                _LOG.warning("MCP IPC error reply failed: %s", type(send_exc).__name__)
        finally:
            with suppress(OSError, EOFError):
                connection.close()

    def start_accept() -> None:
        threading.Thread(target=accept_one, daemon=True).start()

    start_accept()
    try:
        while not stopping.is_set():
            item: object | None = None
            with accepted_lock:
                if accepted:
                    item = accepted.popleft()
            if item is None:
                time.sleep(0.01)
                continue
            if isinstance(item, BaseException):
                if not stopping.is_set():
                    _LOG.warning("MCP IPC accept failed: %s", type(item).__name__)
                start_accept()
                continue
            threading.Thread(target=handle, args=(item,), daemon=True).start()
            start_accept()
    finally:
        listener.close()
        host._ipc_listener = None
        _remove_ipc_socket(address, selected_family)


def start_ipc_thread(host: ToolHost, address: str, authkey: bytes) -> threading.Thread:
    thread = threading.Thread(target=serve_ipc, args=(host, address, authkey), daemon=True)
    host._ipc_thread = thread
    thread.start()
    return thread
