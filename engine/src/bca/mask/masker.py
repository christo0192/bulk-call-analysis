"""Streaming and parallel masking orchestration."""

from __future__ import annotations

import gzip
import json
import os
import sys
import tempfile
from collections.abc import Iterable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .names import HarvestMissing, NerBackend, mask_names, roster_pattern
from .patterns import apply_patterns


class MissingNer(RuntimeError):
    """Masking cannot proceed without a configured NER backend."""


@dataclass(frozen=True)
class _NoopNer:
    def persons(self, text: str) -> list[Any]:
        return []


@dataclass(frozen=True)
class MaskingContext:
    roster_file: Path | None = None
    allow_ids: frozenset[str] = frozenset()
    ner: NerBackend | None = _NoopNer()
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
    failed: bool = False
    error: str | None = None


def load_harvest(job_folder: Path) -> tuple[str, ...]:
    path = job_folder / "harvested_names.json"
    if not path.is_file():
        raise HarvestMissing(f"harvest list is missing: {path.name}")
    values = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise HarvestMissing("harvest list must be a JSON list of strings")
    return tuple(values)


def _validate_context(ctx: MaskingContext) -> None:
    if ctx.ner is None:
        raise MissingNer("a NER backend is required")
    if ctx.harvested_names is None:
        raise HarvestMissing("per-job harvested names are required")


def mask_text(text: str, ctx: MaskingContext) -> Masked:
    _validate_context(ctx)
    current, counts = apply_patterns(text, ctx.allow_ids)
    current, name_counts = mask_names(
        current, roster_pattern(ctx.roster_file), ctx.ner, ctx.harvested_names or ()
    )
    for name, count in name_counts.items():
        counts[name] = counts.get(name, 0) + count
    return Masked(current, counts)


def _mask_value(value: Any, ctx: MaskingContext, counts: dict[str, int]) -> Any:
    if isinstance(value, str):
        masked = mask_text(value, ctx)
        for name, count in masked.counts_by_detector.items():
            counts[name] = counts.get(name, 0) + count
        return masked.text
    if isinstance(value, dict):
        return {key: _mask_value(item, ctx, counts) for key, item in value.items()}
    if isinstance(value, list):
        return [_mask_value(item, ctx, counts) for item in value]
    return value


def _mask_record(record: dict[str, Any], ctx: MaskingContext) -> tuple[dict[str, Any], dict[str, int]]:
    counts: dict[str, int] = {}
    return _mask_value(record, ctx, counts), counts


def mask_lead_stream(raw_file: Path, out_file: Path, ctx: MaskingContext) -> LeadMaskStats:
    _validate_context(ctx)
    counts: dict[str, int] = {}
    calls = 0
    out_file.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{out_file.name}.", suffix=".tmp", dir=out_file.parent)
    os.close(fd)
    temp_file = Path(temp_name)
    try:
        with (
            gzip.open(raw_file, "rt", encoding="utf-8") as source,
            gzip.open(temp_file, "wt", encoding="utf-8", newline="\n") as target,
        ):
            for line in source:
                if not line.strip():
                    continue
                record, record_counts = _mask_record(json.loads(line), ctx)
                target.write(json.dumps(record, ensure_ascii=False) + "\n")
                calls += 1
                for name, count in record_counts.items():
                    counts[name] = counts.get(name, 0) + count
        os.replace(temp_file, out_file)
    except Exception:
        temp_file.unlink(missing_ok=True)
        raise
    return LeadMaskStats(calls, counts)


def _lower_priority(priority: str) -> None:
    if priority == "normal":
        return
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.SetPriorityClass.restype = wintypes.BOOL
        classes = {"below_normal": 0x4000, "low": 0x1000}
        if not kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), classes.get(priority, 0x4000)):
            raise OSError("SetPriorityClass failed")
    elif hasattr(os, "nice"):
        os.nice(10)


_WORKER_CONTEXT: MaskingContext | None = None


def _init_worker(ctx: MaskingContext, priority: str) -> None:
    global _WORKER_CONTEXT
    _lower_priority(priority)
    _validate_context(ctx)
    _WORKER_CONTEXT = ctx
    roster_pattern(ctx.roster_file)


def _mask_one(item: tuple[Path, Path]) -> LeadMaskStats:
    if _WORKER_CONTEXT is None:
        raise RuntimeError("masking worker was not initialized")
    raw, out = item
    try:
        return mask_lead_stream(raw, out, _WORKER_CONTEXT)
    except Exception as exc:
        return LeadMaskStats(0, {}, failed=True, error=f"{type(exc).__name__}: {exc}")


def run_masking(
    leads: Iterable[tuple[Path, Path] | tuple[Path, Path, MaskingContext]],
    workers: int,
    max_tasks_per_child: int = 50,
    priority: str = "below_normal",
) -> list[LeadMaskStats]:
    jobs: list[tuple[Path, Path]] = []
    contexts: list[MaskingContext] = []
    for lead in leads:
        if len(lead) == 2:
            raise MissingNer("each masking job must provide a NER-backed context")
        raw, out, ctx = lead
        _validate_context(ctx)
        jobs.append((raw, out))
        contexts.append(ctx)
    if not jobs:
        return []
    if any(ctx != contexts[0] for ctx in contexts[1:]):
        raise ValueError("all leads in one masking run must share a context")
    if workers <= 1:
        global _WORKER_CONTEXT
        _WORKER_CONTEXT = contexts[0]
        return [_mask_one(job) for job in jobs]
    with ProcessPoolExecutor(
        max_workers=workers,
        max_tasks_per_child=max_tasks_per_child,
        initializer=_init_worker,
        initargs=(contexts[0], priority),
    ) as pool:
        return list(pool.map(_mask_one, jobs))
