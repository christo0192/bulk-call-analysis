from datetime import UTC, datetime
from pathlib import Path

from bca.config import RetentionConfig
from bca.setup.retention import sweep


def test_retention_dry_run_and_delete(tmp_path: Path) -> None:
    old = tmp_path / "cache/transcripts/old.txt"
    new = tmp_path / "cache/transcripts/new.txt"
    old.parent.mkdir(parents=True)
    old.write_text("old", encoding="utf-8")
    new.write_text("new", encoding="utf-8")
    now = datetime.now(UTC)
    old.touch()
    import os

    os.utime(old, (now.timestamp() - 3 * 86400, now.timestamp() - 3 * 86400))
    found = sweep(tmp_path, RetentionConfig(1, 90), finished_at=now, dry_run=True)
    assert old in found and old.exists()
    sweep(tmp_path, RetentionConfig(1, 90), finished_at=now)
    assert not old.exists() and new.exists()
