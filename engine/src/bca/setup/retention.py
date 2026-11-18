from __future__ import annotations

import logging
from collections.abc import Collection
from datetime import datetime, timedelta
from pathlib import Path

from bca.config import RetentionConfig

logger = logging.getLogger(__name__)


def _inside(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _safe_before_unlink(root: Path, path: Path) -> bool:
    try:
        relative = path.absolute().relative_to(root.absolute())
    except ValueError:
        return False
    current = root
    for part in relative.parts[:-1]:
        current /= part
        if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
            return False
        try:
            if getattr(current.stat(), "st_file_attributes", 0) & 0x400:
                return False
        except OSError:
            return False
    try:
        if path.is_symlink():
            return False
        attributes = getattr(path.stat(), "st_file_attributes", 0)
        return _inside(root, path) and not bool(attributes & 0x400)
    except OSError:
        return False


def sweep(
    app_data: Path,
    retention: RetentionConfig,
    *,
    finished_at: datetime,
    dry_run: bool = False,
    passed_leads: Collection[str] | None = None,
) -> list[Path]:
    root = app_data.resolve()
    cutoff_masked = finished_at - timedelta(days=retention.masked_cache_days)
    cutoff_chat = finished_at - timedelta(days=retention.chat_days)
    candidates: list[Path] = []
    folders = (
        (root / "raw", cutoff_masked),
        (root / "cache" / "transcripts", cutoff_masked),
        (root / "logs", cutoff_chat),
        (root / "state", cutoff_chat),
    )
    for folder, cutoff in folders:
        if not folder.is_dir():
            continue
        for path in folder.rglob("*"):
            if path.is_symlink():
                continue
            try:
                old_enough = datetime.fromtimestamp(path.stat().st_mtime, tz=finished_at.tzinfo) < cutoff
            except OSError:
                continue
            passed_raw = (
                folder.name == "raw"
                and passed_leads is not None
                and any(
                    path.name.removesuffix(suffix) in passed_leads
                    for suffix in (".jsonl.gz", ".jsonl", ".txt", ".gz")
                )
            )
            if path.is_file() and (passed_raw or old_enough):
                if not _inside(root, path):
                    continue
                if folder.name == "raw" and passed_leads is not None:
                    lead = path.name
                    for suffix in (".jsonl.gz", ".jsonl", ".txt", ".gz"):
                        if lead.endswith(suffix):
                            lead = lead[: -len(suffix)]
                            break
                    if lead not in passed_leads:
                        continue
                candidates.append(path)
    if not dry_run:
        for path in candidates:
            if not _safe_before_unlink(root, path):
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            except OSError as exc:
                logger.warning("retention could not delete %s: %s", path, exc)
    return candidates
