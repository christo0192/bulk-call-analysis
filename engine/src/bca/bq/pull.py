from __future__ import annotations

import gzip
import json
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from bca.config import BigQueryConfig, RecipeConfig
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


def _duration(call: dict[str, object], unit: str) -> float:
    value = call.get("duration")
    if not isinstance(value, (int, float)):
        return 0.0
    return float(value) / 1000 if unit == "ms" else float(value)


def _number(value: object, default: int = 0) -> int:
    return int(value) if isinstance(value, (int, float, str)) else default


def _decimal(value: object, default: float = 0.0) -> float:
    return float(value) if isinstance(value, (int, float, str)) else default


def run_preflight(
    recipe: RecipeConfig,
    usable_rule: UsableRule,
    scope: str,
    window: tuple[str, str] | None,
    lead_ids: list[str],
    runner: BqRunner,
    store: Store,
    job_id: str,
) -> Preflight:
    sql, params, _ = preflight_sql(recipe, usable_rule, scope, window)
    params["lead_ids"] = lead_ids
    dry = runner.dry_run(sql, params)
    rows = [row for page in runner.iter_pages(sql, params, 1000) for row in page]
    by_id = {str(row["lead_id"]): row for row in rows}
    if set(by_id) != set(lead_ids):
        raise RuntimeError("preflight did not return exactly one result per requested lead")
    buckets = {
        name: 0 for name in ("usable", "no_transcript", "transcription_failed", "too_short", "not_found")
    }
    per_lead: dict[str, str] = {}
    for lead_id in lead_ids:
        row = by_id[lead_id]
        found = bool(row.get("found"))
        calls = _number(row.get("calls", 0))
        usable = _number(row.get("usable_calls", 0))
        failed = _number(row.get("transcription_failed", 0))
        bucket = (
            "not_found"
            if not found
            else "usable"
            if usable
            else "transcription_failed"
            if failed
            else "no_transcript"
            if calls == 0
            else "too_short"
        )
        buckets[bucket] += 1
        per_lead[lead_id] = bucket
        if bucket != "usable":
            status = LeadStatus(bucket)
            store.set_lead_status(job_id, lead_id, status)
    if sum(buckets.values()) != len(lead_ids):
        raise RuntimeError("preflight bucket counts do not reconcile")
    return Preflight(buckets, per_lead, dry.bytes)


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
) -> PullStats:
    sql, params, _ = pull_sql(recipe, scope, window)
    params["lead_ids"] = lead_ids
    dry = runner.dry_run(sql, params)
    if dry.bytes > cfg.reject_bytes and (approved_bytes is None or approved_bytes < dry.bytes):
        raise PermissionError("scan exceeds reject_bytes without operator approval")
    raw_dir = out_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, object] = {}
    counts: dict[str, int] = {lead_id: 0 for lead_id in lead_ids}
    with ExitStack() as stack:
        for page in runner.iter_pages(sql, params, page_size):
            for call in page:
                lead_id = str(call["lead_id"])
                handle = files.get(lead_id)
                if handle is None:
                    handle = stack.enter_context(
                        gzip.open(raw_dir / f"{lead_id}.jsonl.gz", "wt", encoding="utf-8")
                    )
                    files[lead_id] = handle
                assert hasattr(handle, "write")
                handle.write(json.dumps(call, default=str, ensure_ascii=False) + "\n")
                counts[lead_id] = counts.get(lead_id, 0) + 1
    for lead_id, count in counts.items():
        store.set_lead_status(job_id, lead_id, LeadStatus.pulled)
        with store.transaction() as cur:
            cur.execute(
                "UPDATE leads SET calls_pulled=? WHERE job_id=? AND lead_id=?", (count, job_id, lead_id)
            )
    return PullStats(sum(counts.values()), sum(1 for count in counts.values() if count), dry.bytes)


def build_bundle(lead_id: str, calls: Iterable[dict[str, object]], cap: CapRule) -> Bundle:
    ordered = sorted(calls, key=lambda call: str(call.get("call_start", "")))
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
                (i for i, call in enumerate(kept) if _decimal(call.get("duration", 0)) < cap.drop_short_s),
                None,
            )
            index = 0 if short is None else short
            dropped.append(str(kept.pop(index).get("call_id", "")))
    blocks: list[str] = []
    for number, call in enumerate(kept, 1):
        start = call.get("call_start", "")
        if isinstance(start, (datetime, date)):
            start = start.isoformat()
        duration = call.get("duration", 0)
        blocks.append(
            f"=== CALL {number} of {len(kept)} | {start} | {duration} s ===\n"
            f"{call.get('transcript_text', '')}"
        )
    return Bundle("\n\n".join(blocks), len(kept), dropped, chars(kept))
