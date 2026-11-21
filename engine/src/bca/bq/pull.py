from __future__ import annotations

import gzip
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from bca.config import BigQueryConfig, RecipeConfig
from bca.stop import StopRequested
from bca.store import LeadStatus, Store
from bca.store.spec import CapRule, UsableRule

from .queries import preflight_sql, pull_sql
from .runner import BqRunner

if TYPE_CHECKING:
    from collections.abc import Iterable


@dataclass(frozen=True)
class Preflight:
    buckets: dict[str, int]
    per_lead: dict[str, str]
    dry_run_bytes: int
    capped_leads: int


@dataclass(frozen=True)
class PullStats:
    calls: int
    leads: int
    dry_run_bytes: int


@dataclass(frozen=True)
class Bundle:
    text: str
    calls_sent: int
    calls_dropped: list[str]
    chars: int


def _scan_allowed(dry_bytes: int, reject_bytes: int, approved_bytes: int | None) -> bool:
    if dry_bytes <= reject_bytes:
        return True
    return approved_bytes is not None and dry_bytes <= int(approved_bytes * 1.10)


def _scan_error(dry_bytes: int, approved_bytes: int | None) -> PermissionError:
    if approved_bytes is None:
        return PermissionError("scan exceeds reject_bytes without operator approval")
    return PermissionError(
        f"scan is {dry_bytes} bytes, above the approved tolerance of {int(approved_bytes * 1.10)} bytes"
    )


def _duration(call: dict[str, object], unit: str) -> float:
    value = call.get("duration")
    if not isinstance(value, (int, float)):
        return 0.0
    return float(value) / 1000 if unit == "ms" else float(value)


def _number(value: object, default: int = 0) -> int:
    return int(value) if isinstance(value, (int, float, str)) else default


def run_preflight(
    recipe: RecipeConfig,
    usable_rule: UsableRule,
    scope: str,
    window: tuple[str, str] | None,
    lead_ids: list[str],
    runner: BqRunner,
    store: Store,
    job_id: str,
    cfg: BigQueryConfig,
    approved_bytes: int | None = None,
    persist: bool = True,
    cap: CapRule | None = None,
    stop_requested: Callable[[], bool] | None = None,
) -> Preflight:
    sql, params, _ = preflight_sql(recipe, usable_rule, scope, window)
    params["lead_ids"] = lead_ids
    dry = runner.dry_run(sql, params)
    if not _scan_allowed(dry.bytes, cfg.reject_bytes, approved_bytes):
        raise _scan_error(dry.bytes, approved_bytes)
    rows = []
    for page in runner.iter_pages(sql, params, 1000):
        if stop_requested is not None and stop_requested():
            raise StopRequested
        rows.extend(page)
    by_id = {str(row["lead_id"]): row for row in rows}
    if set(by_id) != set(lead_ids):
        raise RuntimeError("preflight did not return exactly one result per requested lead")
    buckets = {
        name: 0 for name in ("usable", "no_transcript", "transcription_failed", "too_short", "not_found")
    }
    per_lead: dict[str, str] = {}
    capped_leads = 0
    for lead_id in lead_ids:
        row = by_id[lead_id]
        found = bool(row.get("found"))
        _number(row["calls"])
        transcript_calls = _number(row["transcript_calls"])
        usable = _number(row["usable_calls"])
        failed = _number(row["transcription_failed"])
        bucket = (
            "not_found"
            if not found
            else "usable"
            if usable
            else "transcription_failed"
            if failed
            else "no_transcript"
            if transcript_calls == 0
            else "too_short"
        )
        buckets[bucket] += 1
        per_lead[lead_id] = bucket
        if cap is not None and _number(row.get("transcript_chars")) > cap.max_chars:
            capped_leads += 1
        if bucket != "usable" and persist:
            status = LeadStatus(bucket)
            store.set_lead_status(job_id, lead_id, status)
            with store.transaction() as cur:
                cur.execute(
                    "UPDATE leads SET calls_dropped_unusable=? WHERE job_id=? AND lead_id=?",
                    (max(0, _number(row["calls"]) - usable), job_id, lead_id),
                )
    if sum(buckets.values()) != len(lead_ids):
        raise RuntimeError("preflight bucket counts do not reconcile")
    return Preflight(buckets, per_lead, dry.bytes, capped_leads)


def pull_transcripts(
    cfg: BigQueryConfig,
    recipe: RecipeConfig,
    lead_ids: list[str],
    runner: BqRunner,
    store: Store,
    job_id: str,
    out_dir: Path,
    scope: str = "all",
    window: tuple[str, str] | None = None,
    approved_bytes: int | None = None,
    page_size: int = 1000,
    stop_requested: Callable[[], bool] | None = None,
) -> PullStats:
    sql, params, _ = pull_sql(recipe, scope, window)
    params["lead_ids"] = lead_ids
    dry = runner.dry_run(sql, params)
    if not _scan_allowed(dry.bytes, cfg.reject_bytes, approved_bytes):
        raise _scan_error(dry.bytes, approved_bytes)
    raw_dir = out_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {lead_id: 0 for lead_id in lead_ids}
    current_id: str | None = None
    handle = None
    requested = set(lead_ids)
    try:
        for page in runner.iter_pages(sql, params, page_size):
            if stop_requested is not None and stop_requested():
                raise StopRequested
            for call in page:
                if stop_requested is not None and stop_requested():
                    raise StopRequested
                lead_id = str(call["lead_id"])
                if lead_id not in requested:
                    raise RuntimeError(f"pull returned unrequested lead {lead_id!r}")
                if current_id != lead_id:
                    if handle is not None:
                        handle.close()
                    handle = gzip.open(  # noqa: SIM115
                        raw_dir / f"{lead_id}.jsonl.gz", "wt", encoding="utf-8"
                    )
                    current_id = lead_id
                assert handle is not None
                handle.write(json.dumps(call, default=str, ensure_ascii=False) + "\n")
                counts[lead_id] += 1
    finally:
        if handle is not None:
            handle.close()
    with store.transaction() as cur:
        for lead_id, count in counts.items():
            existing = cur.execute(
                "SELECT status FROM leads WHERE job_id=? AND lead_id=?", (job_id, lead_id)
            ).fetchone()
            status = str(existing[0]) if existing is not None else str(LeadStatus.pending)
            if status in {LeadStatus.pending, LeadStatus.pulled}:
                status = str(LeadStatus.pulled if count else LeadStatus.no_transcript)
            cur.execute(
                "UPDATE leads SET status=?, calls_pulled=? WHERE job_id=? AND lead_id=?",
                (status, count, job_id, lead_id),
            )
    return PullStats(sum(counts.values()), sum(1 for count in counts.values() if count), dry.bytes)


def _start_key(value: object) -> tuple[int, object]:
    if value is None or value == "":
        return (1, "")
    if isinstance(value, (datetime, date)):
        return (0, value.isoformat())
    if isinstance(value, (int, float)):
        return (0, value)
    try:
        return (0, datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return (0, str(value))


def build_bundle(
    lead_id: str, calls: Iterable[dict[str, object]], cap: CapRule, duration_unit: str = "s"
) -> Bundle:
    ordered = sorted(calls, key=lambda call: _start_key(call.get("call_start")))
    dropped: list[str] = []

    def chars(items: list[dict[str, object]]) -> int:
        return sum(len(str(item.get("transcript_text", ""))) for item in items)

    kept = list(ordered)
    if cap.policy == "keep_newest":
        while kept and chars(kept) > cap.max_chars:
            dropped.insert(0, str(kept.pop(0).get("call_id", "")))
    else:
        while kept and chars(kept) > cap.max_chars:
            short = next(
                (i for i, call in enumerate(kept) if _duration(call, duration_unit) < cap.drop_short_s),
                None,
            )
            index = 0 if short is None else short
            dropped.append(str(kept.pop(index).get("call_id", "")))
    blocks: list[str] = []
    for number, call in enumerate(kept, 1):
        start = call.get("call_start", "")
        if isinstance(start, (datetime, date)):
            start = start.isoformat()
        duration = _duration(call, duration_unit)
        blocks.append(
            f"=== CALL {number} of {len(kept)} | {start} | {duration} s ===\n"
            f"{call.get('transcript_text', '')}"
        )
    return Bundle("\n\n".join(blocks), len(kept), dropped, chars(kept))
