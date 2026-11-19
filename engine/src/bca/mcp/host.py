from __future__ import annotations

import gzip
import hmac
import json
import logging
import math
import re
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from queue import Empty, Full, Queue
from typing import Any, Protocol, cast

from bca.bq.guard import sql_tokens, validate_select
from bca.bq.leads import record_lead_intake, resolve_lead_source
from bca.bq.pull import build_bundle, run_preflight
from bca.bq.runner import BqRunner
from bca.config import BigQueryConfig, ModelsConfig, RecipeConfig
from bca.mask.masker import MaskingContext, _NoopNer, mask_text
from bca.monitor.eta import EtaEstimator
from bca.monitor.progress import counts as progress_counts
from bca.outputs.aggregates import aggregates
from bca.outputs.rules import validate_rules
from bca.store import JobSpec, LeadStatus, Store, load_latest_spec, save_spec
from bca.store.lifecycle import JobState, edit_spec, transition

from .wire import (
    HANDSHAKE_TIMEOUT,
    MAX_REQUEST_BYTES,
    OK,
    REQUEST_TIMEOUT,
    decode_json,
    encode_json,
    expected_mac,
    ipc_family,
    make_hello,
)

MAX_RESULT_CHARS = 40_000
BQ_QUERY_DEADLINE_S = 90.0
_LOG = logging.getLogger("bca.mcp")
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PATH_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$", re.ASCII)
_VIEWS = {"TABLES", "COLUMNS", "VIEWS", "TABLE_OPTIONS", "COLUMN_FIELD_PATHS", "PARTITIONS"}
_FORMAT_SPECIFIER = re.compile(r"%[-+ #'0]*(\d+|\*)?(\.(\d+|\*))?[xXo]")


def _has_forbidden_reconstruction(sql: str) -> bool:
    return _has_forbidden_from_tokens(sql)


def _has_forbidden_from_tokens(sql: str) -> bool:
    tokens = sql_tokens(sql)
    words = {
        "TO_CODE_POINTS",
        "CODE_POINTS_TO_STRING",
        "CODE_POINTS_TO_BYTES",
        "TO_HEX",
        "FROM_HEX",
        "TO_BASE64",
        "FROM_BASE64",
        "TO_BASE32",
        "FROM_BASE32",
        "ASCII",
        "CHR",
        "UNICODE",
        "REVERSE",
    }
    for index, token in enumerate(tokens):
        if (
            token.kind == "word"
            and token.text.upper() in words
            and index + 1 < len(tokens)
            and tokens[index + 1].text == "("
        ):
            return True
        if token.kind == "word" and token.text.upper() == "FORMAT":
            if index + 2 >= len(tokens) or tokens[index + 1].text != "(":
                continue
            format_token = tokens[index + 2]
            if format_token.kind != "literal":
                return True
            if index + 3 >= len(tokens) or tokens[index + 3].text != ",":
                return True
            literal = _literal_contents(format_token.text)
            if literal is None:
                return True
            if _FORMAT_SPECIFIER.search(literal):
                return True
    return False


class ToolHostError(RuntimeError):
    """A short, safe error intended for an MCP client."""


class ListableRunner(Protocol):
    def list_datasets(self) -> list[dict[str, object]]: ...
    def list_tables(self, dataset: str) -> list[dict[str, object]]: ...
    def describe_table(self, dataset: str, table: str) -> dict[str, object]: ...


def _masked(
    value: object,
    context: MaskingContext,
) -> object:
    if isinstance(value, str):
        if _is_pure_numeric(value) and _is_sensitive_number(value):
            return "[NUMBER]"
        return mask_text(value, context).text
    if isinstance(value, list):
        return [_masked(item, context) for item in value]
    if isinstance(value, tuple):
        return [_masked(item, context) for item in value]
    if isinstance(value, dict):
        merged: dict[str, object] = {}
        for key, item in value.items():
            masked_key = str(_masked(str(key), context))
            masked_value = _masked(item, context)
            if masked_key in merged and isinstance(merged[masked_key], int) and isinstance(masked_value, int):
                merged[masked_key] = cast(int, merged[masked_key]) + masked_value
            else:
                merged[masked_key] = masked_value
        return merged
    if isinstance(value, bytes):
        return f"[BYTES {len(value)}]"
    if isinstance(value, int) and not isinstance(value, bool):
        digits = str(abs(value))
        normalized = _normalized_number_digits(digits)
        if 10 <= len(normalized) <= 13:
            return "[NUMBER]"
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return "NaN" if math.isnan(value) else ("Infinity" if value > 0 else "-Infinity")
        if _is_sensitive_number(str(int(value))):
            return "[NUMBER]"
        return value
    if value is None or isinstance(value, bool):
        return value
    return mask_text(str(value)[:MAX_RESULT_CHARS], context).text


def _cap(
    value: object,
    context: MaskingContext,
    *,
    unit: str = "characters",
    already_masked: bool = False,
) -> object:
    if isinstance(value, str):
        masked_text = value if already_masked else mask_text(value, context).text
        if len(value) <= MAX_RESULT_CHARS and len(masked_text) <= MAX_RESULT_CHARS:
            return masked_text
        marker_template = "\n[... {} {} omitted ...]"
        omitted = max(0, len(value) - MAX_RESULT_CHARS)
        marker = marker_template.format(omitted, unit)
        string_kept = max(0, MAX_RESULT_CHARS - len(marker))
        omitted = len(value) - string_kept
        marker = marker_template.format(omitted, unit)
        return masked_text[:string_kept] + marker
    masked = value if already_masked else _masked(value, context)
    if isinstance(masked, str) and len(masked) > MAX_RESULT_CHARS:
        omitted = len(masked) - MAX_RESULT_CHARS
        return masked[: MAX_RESULT_CHARS - 80] + f"\n[truncated: {omitted} {unit} omitted]"
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
    if isinstance(masked, dict):
        result = dict(masked)
        original_counts: dict[str, int] = {}
        while len(json.dumps(result, ensure_ascii=False, default=str)) > MAX_RESULT_CHARS:
            candidates = [
                (len(json.dumps(item, ensure_ascii=False, default=str)), key, item)
                for key, item in result.items()
                if isinstance(item, (list, dict)) and item
            ]
            if not candidates:
                break
            _, key, item = sorted(candidates)[-1]
            if isinstance(item, list):
                kept_count = len(item) // 2
                original_count = original_counts.setdefault(key, len(item))
                result[key] = item[:kept_count]
            else:
                entries = list(item.items())
                kept_count = len(entries) // 2
                original_count = original_counts.setdefault(key, len(entries))
                result[key] = dict(entries[:kept_count])
            result[f"{key}_truncated"] = f"{original_count - kept_count} of {original_count} entries omitted"
        encoded_result = json.dumps(result, ensure_ascii=False, default=str)
        if len(encoded_result) <= MAX_RESULT_CHARS:
            return result
        # A single large scalar still needs a hard cap.
        return encoded_result[: MAX_RESULT_CHARS - 80] + "\n[truncated result]"
    omitted = len(encoded) - MAX_RESULT_CHARS
    return {"items": [], "truncated": f"{omitted} {unit} omitted"}


def _normalized_number_digits(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if digits.startswith("91"):
        return digits[2:]
    if digits.startswith("0"):
        return digits[1:]
    return digits


def _is_sensitive_number(value: str) -> bool:
    match = re.fullmatch(r"[+-]?[\d][\d\s,._-]*(?:\.\d+)?", value)
    return match is not None and 10 <= len(_normalized_number_digits(match.group(0))) <= 13


def _is_pure_numeric(value: str) -> bool:
    return re.fullmatch(r"[+-]?[\d][\d\s,._-]*(?:\.\d+)?", value) is not None


def _literal_contents(token: str) -> str | None:
    match = re.match(r"(?i)(?:rb|br|r|b)?(['\"]{1,3})", token)
    if match is None:
        return None
    delimiter = match.group(1)
    if not token.endswith(delimiter) or len(token) < len(delimiter) * 2:
        return None
    return token[len(match.group(0)) : -len(delimiter)]


def _format_bytes(value: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    amount = float(value)
    unit = 0
    while abs(amount) >= 1024 and unit < len(units) - 1:
        amount /= 1024
        unit += 1
    decimals = 0 if unit == 0 else 2
    return f"{amount:.{decimals}f} {units[unit]}"


def _pages_until_deadline(runner: BqRunner, sql: str, deadline: float) -> Any:
    messages: Queue[object] = Queue(maxsize=2)
    stop = threading.Event()
    finished = object()

    def produce() -> None:
        iterator: Iterator[list[dict[str, object]]] | None = None
        try:
            iterator = iter(
                runner.iter_pages(sql, page_size=500, timeout=max(0.0, deadline - time.monotonic()))
            )
            for page in iterator:
                while not stop.is_set():
                    try:
                        messages.put(page, timeout=0.05)
                        break
                    except Full:
                        continue
                if stop.is_set():
                    break
        except BaseException as exc:
            while not stop.is_set():
                try:
                    messages.put(exc, timeout=0.05)
                    break
                except Full:
                    continue
        finally:
            if iterator is not None:
                with suppress(BaseException):
                    iterator.close()  # type: ignore[attr-defined]
            while not stop.is_set():
                try:
                    messages.put(finished, timeout=0.05)
                    break
                except Full:
                    continue

    thread = threading.Thread(target=produce, name="bca-bq-query", daemon=True)
    thread.start()
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ToolHostError("BigQuery query timed out")
            try:
                message = messages.get(timeout=remaining)
            except Empty:
                raise ToolHostError("BigQuery query timed out") from None
            if message is finished:
                return
            if isinstance(message, BaseException):
                raise message
            yield cast(list[dict[str, object]], message)
    finally:
        stop.set()


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
        models: ModelsConfig | None = None,
        notes_path: Path | None = None,
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
        self.models = models or ModelsConfig()
        self.notes_path = notes_path or (project_dir / "notes.md")
        self._ipc_stop = threading.Event()
        self._ipc_ready = threading.Event()
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
            return _cap(method(**args), self.mask_context, already_masked=name == "bq_query")
        except BaseException as exc:
            safe = _safe_error(exc, self.mask_context)
            _LOG.warning("MCP tool %s failed: %s", name, type(exc).__name__)
            raise safe from None

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
        return {
            "bytes": _format_bytes(dry.bytes),
            "referenced_tables": dry.referenced_tables,
            "schema": dry.schema,
        }

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
        deadline = time.monotonic() + BQ_QUERY_DEADLINE_S
        for page in _pages_until_deadline(self.runner, sql, deadline):
            if time.monotonic() >= deadline:
                raise ToolHostError("BigQuery query timed out")
            for row in page:
                if time.monotonic() >= deadline:
                    raise ToolHostError("BigQuery query timed out")
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
                    _masked(row, self.mask_context),
                )
                candidate = json.dumps(
                    {"bytes": _format_bytes(dry.bytes), "rows": [*rows, masked]},
                    ensure_ascii=False,
                    default=str,
                )
                if len(candidate) > MAX_RESULT_CHARS:
                    omitted_rows = 1
                    omitted_chars = raw_size
                    break
                rows.append(masked)
            if len(rows) >= max_rows or omitted_rows:
                break
        result: dict[str, object] = {
            "bytes": _format_bytes(dry.bytes),
            "rows": rows,
            "truncated": bool(omitted_rows),
        }
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
        path = self.notes_path
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
        if self.recipe is None:
            raise ToolHostError("a recipe and pre-flight are required before proposing a job")
        if (spec.luna_model, spec.luna_effort) != (self.models.luna_model, self.models.luna_effort):
            raise ToolHostError("luna model and effort must match configured models")
        validate_rules(spec.rules, spec.output_schema)
        job_dir = self._job_dir(spec.job_id)
        if hasattr(spec.lead_source, "ids_file"):
            ids_file = Path(str(cast(Any, spec.lead_source).ids_file))
            if ids_file.is_absolute() or ids_file.name != str(ids_file) or ".." in ids_file.parts:
                raise ToolHostError("invalid lead source path")
        lead_source_dry_run_bytes = 0
        if hasattr(spec.lead_source, "sql"):
            lead_source_dry_run_bytes = self.runner.dry_run(str(cast(Any, spec.lead_source).sql)).bytes
        existing = self.store.rows(
            "SELECT state FROM jobs WHERE id=? AND project_id=?", (spec.job_id, self.project_id)
        )
        owner = self.store.rows("SELECT project_id FROM jobs WHERE id=?", (spec.job_id,))
        if owner and str(owner[0][0]) != self.project_id:
            raise ToolHostError("job id already in use")
        created = not existing
        if existing and str(existing[0][0]) in {
            "full_running",
            "quote_check",
            "building_output",
            "verifying",
            "done",
            "done_with_issues",
        }:
            raise ToolHostError("spec cannot be edited after the full run starts")
        saved_path: Path | None = None
        try:
            intake = resolve_lead_source(
                spec.lead_source,
                self.runner,
                self.store,
                job_dir,
                self.bq_config,
                spec.job_id,
                persist=False,
            )
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
                persist=False,
                cap=spec.cap,
            )
            saved_path = save_spec(spec, job_dir)
            version = int(saved_path.stem.removeprefix("spec.v"))
            if created:
                self.store.create_job(spec.job_id, spec.project_id, version, "draft")
                record_lead_intake(self.store, spec.job_id, intake)
                for lead_id, bucket in preflight.per_lead.items():
                    if bucket != "usable":
                        self.store.set_lead_status(spec.job_id, lead_id, LeadStatus(bucket))
                transition(self.store, spec.job_id, JobState.plan_proposed, "plan proposed")
            else:
                edit_spec(self.store, spec.job_id, version)
                record_lead_intake(self.store, spec.job_id, intake, replace_existing=True)
                for lead_id, bucket in preflight.per_lead.items():
                    if bucket != "usable":
                        self.store.set_lead_status(spec.job_id, lead_id, LeadStatus(bucket))
        except BaseException:
            if saved_path is not None:
                saved_path.unlink(missing_ok=True)
            if created:
                with self.store.transaction() as cur:
                    cur.execute("DELETE FROM leads WHERE job_id=?", (spec.job_id,))
                    cur.execute("DELETE FROM id_intake WHERE job_id=?", (spec.job_id,))
                    cur.execute(
                        "DELETE FROM jobs WHERE id=? AND project_id=?", (spec.job_id, self.project_id)
                    )
            raise
        summary: dict[str, object] = {
            "job_id": spec.job_id,
            "spec_version": version,
            "spec": saved_path.name if saved_path is not None else "",
            "state": "plan_proposed",
            "lead_counts": {
                "raw": intake.raw_count,
                "distinct": len(intake.ids),
                "duplicates": intake.duplicates,
                "malformed": len(intake.malformed),
            },
            "rules": [r.to_dict() for r in spec.rules],
        }
        summary["preflight"] = {
            "buckets": preflight.buckets,
            "dry_run_bytes": _format_bytes(preflight.dry_run_bytes),
            "capped_leads": preflight.capped_leads,
        }
        summary["capped_leads"] = preflight.capped_leads
        summary["lead_source_dry_run_bytes"] = _format_bytes(lead_source_dry_run_bytes)
        return summary

    def job_status(self, job_id: str) -> object:
        self._job_dir(job_id)
        rows = self.store.rows(
            "SELECT state, spec_version FROM jobs WHERE id=? AND project_id=?", (job_id, self.project_id)
        )
        if not rows:
            raise ToolHostError("job not found")
        progress = progress_counts(self.store, job_id)
        errors = self.store.rows(
            "SELECT a.error_code, a.ended_at FROM attempts a JOIN jobs j ON j.id=a.job_id "
            "WHERE a.job_id=? AND j.project_id=? AND a.outcome='failed' "
            "ORDER BY a.ended_at DESC LIMIT 10",
            (job_id, self.project_id),
        )
        quota_rows = self.store.rows(
            "SELECT data_json FROM job_events WHERE job_id=? AND kind='quota' ORDER BY id DESC LIMIT 1",
            (job_id,),
        )
        quota: object = None
        quota_reason = "no quota event has been recorded"
        if quota_rows:
            with suppress(TypeError, json.JSONDecodeError):
                quota = json.loads(str(quota_rows[0][0]))
                quota_reason = ""
        attempt_rows = self.store.rows(
            "SELECT started_at, ended_at FROM attempts WHERE job_id=? AND outcome='ok' "
            "AND ended_at IS NOT NULL ORDER BY ended_at",
            (job_id,),
        )
        estimator = EtaEstimator()
        durations: list[float] = []
        for started, ended in attempt_rows:
            try:
                start_ts = datetime.fromisoformat(str(started)).timestamp()
                end_ts = datetime.fromisoformat(str(ended)).timestamp()
            except ValueError:
                continue
            durations.append(end_ts - start_ts)
            estimator.completed(end_ts)
        eta = (
            estimator.estimate(time.time(), progress.remaining, sample_durations=durations)
            if durations
            else None
        )
        counts = {
            "done": progress.done,
            "in_flight": progress.in_flight,
            "retrying": progress.retrying,
            "failed": progress.failed,
            "held_back": progress.held_back,
            "no_transcript": progress.no_transcript,
            "transcription_failed": progress.transcription_failed,
            "too_short": progress.too_short,
            "capped": progress.capped,
            "not_found": progress.not_found,
            "not_started": progress.not_started,
            "remaining": progress.remaining,
        }
        return {
            "job_id": job_id,
            "state": str(rows[0][0]),
            "spec_version": rows[0][1],
            "counts": counts,
            "checklist": [{"name": item.name, "status": item.status} for item in progress.checklist],
            "eta": eta.__dict__ if eta is not None else None,
            "eta_reason": "no completed attempts with timing data" if eta is None else "",
            "quota": quota,
            "quota_reason": quota_reason,
            "recent_errors": [{"error_code": r[0], "ended_at": r[1]} for r in errors],
        }

    def sample_results(self, job_id: str, offset: int = 0, limit: int = 20) -> object:
        if offset < 0 or limit < 1 or limit > 100:
            raise ToolHostError("invalid sample page")
        self._job_dir(job_id)
        sample_rows = self.store.rows(
            "SELECT l.lead_id FROM leads l JOIN jobs j ON j.id=l.job_id "
            "WHERE l.job_id=? AND j.project_id=? AND l.in_sample=1 ORDER BY l.lead_id",
            (job_id, self.project_id),
        )
        refs = {str(row[0]): f"S{index}" for index, row in enumerate(sample_rows, 1)}
        rows = self.store.rows(
            "SELECT r.lead_id, r.answer_json, r.quotes_ok, r.flags_json "
            "FROM results r JOIN leads l USING(job_id, lead_id) JOIN jobs j ON j.id=r.job_id "
            "WHERE r.job_id=? AND j.project_id=? AND l.in_sample=1 ORDER BY r.lead_id LIMIT ? OFFSET ?",
            (job_id, self.project_id, limit, offset),
        )
        return [
            {
                "ref": refs[str(row[0])],
                **{key: row[key] for key in row.keys() if key != "lead_id"},  # noqa: SIM118
            }
            for index, row in enumerate(rows)
        ]

    def lead_transcript(self, job_id: str, ref: str, call_from: int = 1, call_to: int = 3) -> str:
        if call_from < 1 or call_to < call_from:
            raise ToolHostError("invalid call range")
        self._job_dir(job_id)
        match = re.fullmatch(r"S([1-9][0-9]*)", ref)
        if match is None:
            raise ToolHostError("invalid sample reference")
        sample_index = int(match.group(1))
        row = self.store.rows(
            "SELECT l.lead_id FROM leads l JOIN jobs j ON j.id=l.job_id "
            "WHERE l.job_id=? AND j.project_id=? AND l.in_sample=1 "
            "ORDER BY l.lead_id LIMIT 1 OFFSET ?",
            (job_id, self.project_id, sample_index - 1),
        )
        if not row:
            raise ToolHostError("sample reference is unavailable")
        lead_id = str(row[0][0])
        status_row = self.store.rows(
            "SELECT status FROM leads WHERE job_id=? AND lead_id=?", (job_id, lead_id)
        )
        if not status_row or str(status_row[0][0]) not in {"masked", "queued", "in_flight", "done", "failed"}:
            raise ToolHostError("sample reference is unavailable")
        spec, _ = load_latest_spec(self._job_dir(job_id))
        path = self._job_dir(job_id) / "cache" / "transcripts" / f"{lead_id}.jsonl.gz"
        if not path.is_file():
            raise ToolHostError("masked transcript not found")
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            calls = [cast(dict[str, object], json.loads(line)) for line in handle if line.strip()]
        bundle = build_bundle(lead_id, calls, spec.cap)
        blocks = re.findall(r"(?ms)^=== CALL .*?(?=\n\n=== CALL |\Z)", bundle.text)
        selected = blocks[call_from - 1 : call_to]
        return "\n\n".join(selected) or "no calls in requested range"

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
        deadline = time.monotonic() + 5
        self._ipc_stop.set()
        address = self._ipc_address
        family = self._ipc_family
        listener = self._ipc_listener
        if address is not None and family is not None:
            _wake_ipc_listener(address, family)
        if listener is not None:
            with suppress(OSError):
                cast(Any, listener).close()
        thread = self._ipc_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0, deadline - time.monotonic()))
        if address is not None and family is not None:
            _remove_ipc_socket(address, family)
        if thread is not None and thread is not threading.current_thread() and thread.is_alive():
            _LOG.warning("IPC listener is still stopping after the shutdown deadline")


def _wake_ipc_listener(address: str, family: str) -> None:
    """Wake the raw accept with a connection that does not authenticate."""
    try:
        from multiprocessing.connection import Client

        connection = Client(address, family=family, authkey=None)
        connection.close()
    except OSError:
        pass


def _recv_bytes_with_deadline(connection: Any, timeout: float, maxsize: int) -> bytes:
    """Bound a complete frame, including peers that send only its prefix."""
    timer = threading.Timer(timeout, connection.close)
    timer.daemon = True
    timer.start()
    try:
        return cast(bytes, connection.recv_bytes(maxsize))
    finally:
        timer.cancel()


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

    selected_family = family or ipc_family()
    stopping = stop_event or host._ipc_stop
    host._ipc_address = address
    host._ipc_family = selected_family
    host._ipc_authkey = authkey
    if host._ipc_thread is None:
        host._ipc_thread = threading.current_thread()
    listener: Any
    listener = Listener(address, family=selected_family, backlog=16, authkey=None)
    host._ipc_listener = listener
    host._ipc_ready.set()
    if selected_family == "AF_UNIX":
        with suppress(OSError):
            Path(address).chmod(0o600)
    workers = ThreadPoolExecutor(max_workers=4, thread_name_prefix="bca-ipc")
    worker_slots = threading.BoundedSemaphore(4)

    def handle(connection: Any) -> None:
        try:
            hello, _ = make_hello(authkey)
            connection.send_bytes(hello)
            if not connection.poll(HANDSHAKE_TIMEOUT):
                return
            mac = _recv_bytes_with_deadline(connection, HANDSHAKE_TIMEOUT, 64)
            if not hmac.compare_digest(mac, expected_mac(authkey, hello)):
                return
            connection.send_bytes(OK)
            if stopping.is_set() or not connection.poll(REQUEST_TIMEOUT):
                return
            request = decode_json(_recv_bytes_with_deadline(connection, REQUEST_TIMEOUT, MAX_REQUEST_BYTES))
            if not isinstance(request, dict):
                raise ToolHostError("invalid IPC request")
            requested_project = request.get("project_id")
            if requested_project is not None and requested_project != host.project_id:
                raise ToolHostError("project is not authorized for this engine")
            result = host.call(
                str(request.get("tool", "")), cast(Mapping[str, object], request.get("arguments", {}))
            )
            try:
                connection.send_bytes(encode_json({"ok": True, "result": result}))
            except (OSError, EOFError) as exc:
                _LOG.warning("MCP IPC send failed: %s", type(exc).__name__)
        except (OSError, EOFError, ValueError, UnicodeError) as exc:
            _LOG.warning("MCP IPC connection failed: %s", type(exc).__name__)
        except TimeoutError:
            _LOG.warning("MCP IPC connection timed out")
        except BaseException as exc:
            try:
                connection.send_bytes(
                    encode_json({"ok": False, "error": str(_safe_error(exc, host.mask_context))})
                )
            except (OSError, EOFError) as send_exc:
                _LOG.warning("MCP IPC error reply failed: %s", type(send_exc).__name__)
        finally:
            with suppress(OSError, EOFError):
                connection.close()

    accept_failures = 0
    try:
        while not stopping.is_set():
            if not worker_slots.acquire(timeout=0.1):
                continue
            try:
                connection = listener.accept()
            except (OSError, EOFError) as exc:
                worker_slots.release()
                if stopping.is_set():
                    break
                accept_failures += 1
                _LOG.warning("MCP IPC accept failed: %s", type(exc).__name__)
                stopping.wait(min(0.1 * (2 ** min(accept_failures - 1, 4)), 2.0))
                continue
            accept_failures = 0
            if stopping.is_set():
                with suppress(OSError, EOFError):
                    connection.close()
                worker_slots.release()
                break

            def run_worker(connection: Any = connection) -> None:
                try:
                    handle(connection)
                finally:
                    worker_slots.release()

            try:
                workers.submit(run_worker)
            except RuntimeError:
                worker_slots.release()
                with suppress(OSError, EOFError):
                    connection.close()
                raise
    finally:
        with suppress(OSError):
            listener.close()
        workers.shutdown(wait=True, cancel_futures=True)
        host._ipc_listener = None
        _remove_ipc_socket(address, selected_family)


def start_ipc_thread(host: ToolHost, address: str, authkey: bytes) -> threading.Thread:
    host._ipc_ready.clear()
    thread = threading.Thread(target=serve_ipc, args=(host, address, authkey), name="bca-ipc-listener")
    host._ipc_thread = thread
    thread.start()
    if not host._ipc_ready.wait(timeout=5):
        raise RuntimeError("IPC listener did not become ready")
    return thread
