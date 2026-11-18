from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from bca.config import RetentionConfig


def _inside(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def sweep(
    app_data: Path, retention: RetentionConfig, *, finished_at: datetime, dry_run: bool = False
) -> list[Path]:
    root = app_data.resolve()
    cutoff_masked = finished_at - timedelta(days=retention.masked_cache_days)
    cutoff_chat = finished_at - timedelta(days=retention.chat_days)
    candidates: list[Path] = []
    for folder, cutoff in (
        (root / "cache" / "transcripts", cutoff_masked),
        (root / "raw", cutoff_masked),
        (root / "logs", cutoff_chat),
        (root / "state", cutoff_chat),
        (root / "codex", cutoff_chat),
    ):
        if not folder.is_dir():
            continue
        for path in folder.rglob("*"):
            if (
                path.is_file()
                and datetime.fromtimestamp(path.stat().st_mtime, tz=finished_at.tzinfo) < cutoff
            ):
                if not _inside(root, path):
                    raise RuntimeError(f"retention target escapes app data: {path}")
                candidates.append(path)
    if not dry_run:
        for path in candidates:
            path.unlink()
    return candidates
