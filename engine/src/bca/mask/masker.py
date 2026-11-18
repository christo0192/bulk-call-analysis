"""Streaming and parallel masking orchestration."""

from __future__ import annotations

import gzip
import json
import os
import platform
from collections.abc import Iterable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .names import HarvestMissing, NerBackend, mask_names, roster_pattern
from .patterns import apply_patterns


@dataclass(frozen=True)
class MaskingContext:
    roster_file: Path | None = None
    allow_ids: frozenset[str] = frozenset()
    ner: NerBackend | None = None
    harvested_names: tuple[str, ...] | None = ()
    require_harvest: bool = False


@dataclass(frozen=True)
class Masked:
    text: str
    counts_by_detector: dict[str, int]


@dataclass(frozen=True)
class LeadMaskStats:
    calls: int
    detector_counts: dict[str, int]


def mask_text(text: str, ctx: MaskingContext) -> Masked:
    if ctx.require_harvest and ctx.harvested_names is None:
        raise HarvestMissing("per-job harvested names are required but missing")
    current, counts = apply_patterns(text, ctx.allow_ids)
    current, name_counts = mask_names(
        current, roster_pattern(ctx.roster_file), ctx.ner, ctx.harvested_names or ()
    )
    for name, count in name_counts.items():
        counts[name] = counts.get(name, 0) + count
    return Masked(current, counts)


def _mask_record(record: dict[str, Any], ctx: MaskingContext) -> tuple[dict[str, Any], dict[str, int]]:
    for key in ("transcript_text", "text"):
        value = record.get(key)
        if isinstance(value, str):
            masked = mask_text(value, ctx)
            record[key] = masked.text
            return record, masked.counts_by_detector
    return record, {}


def mask_lead_stream(raw_file: Path, out_file: Path, ctx: MaskingContext) -> LeadMaskStats:
    counts: dict[str, int] = {}
    calls = 0
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with (
        gzip.open(raw_file, "rt", encoding="utf-8") as source,
        gzip.open(out_file, "wt", encoding="utf-8", newline="\n") as target,
    ):
        for line in source:
            if not line.strip():
                continue
            record, record_counts = _mask_record(json.loads(line), ctx)
            target.write(json.dumps(record, ensure_ascii=False) + "\n")
            calls += 1
            for name, count in record_counts.items():
                counts[name] = counts.get(name, 0) + count
    return LeadMaskStats(calls, counts)


def _lower_priority(priority: str) -> None:
    if priority == "normal":
        return
    if platform.system() == "Windows":
        import ctypes

        handle = ctypes.windll.kernel32.GetCurrentProcess()
        classes = {"below_normal": 0x4000, "low": 0x1000}
        if not ctypes.windll.kernel32.SetPriorityClass(handle, classes.get(priority, 0x4000)):
            raise OSError("SetPriorityClass failed")
    else:
        nice = getattr(os, "nice", None)
        if nice is not None:
            nice(10)


def _mask_one(item: tuple[Path, Path, MaskingContext, str]) -> LeadMaskStats:
    raw, out, ctx, priority = item
    _lower_priority(priority)
    return mask_lead_stream(raw, out, ctx)


def run_masking(
    leads: Iterable[tuple[Path, Path] | tuple[Path, Path, MaskingContext]],
    workers: int,
    max_tasks_per_child: int = 50,
    priority: str = "below_normal",
) -> list[LeadMaskStats]:
    jobs: list[tuple[Path, Path, MaskingContext, str]] = []
    for lead in leads:
        if len(lead) == 2:
            raw, out = lead
            ctx = MaskingContext()
        else:
            raw, out, ctx = lead
        jobs.append((raw, out, ctx, priority))
    if workers <= 1:
        return [_mask_one(job) for job in jobs]
    with ProcessPoolExecutor(max_workers=workers, max_tasks_per_child=max_tasks_per_child) as pool:
        return list(pool.map(_mask_one, jobs))
