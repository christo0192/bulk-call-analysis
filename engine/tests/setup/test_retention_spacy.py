import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from bca.config import RetentionConfig
from bca.setup.retention import sweep
from bca.setup.spacy_model import MODEL_WHEEL_URL, SpacyModelError, ensure_model


def _old(path: Path, now: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("synthetic", encoding="utf-8")
    timestamp = now.timestamp() - 3 * 86400
    os.utime(path, (timestamp, timestamp))


def test_old_caches_deleted_new_kept_and_dry_run_lists(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    old = tmp_path / "cache/transcripts/old.txt"
    new = tmp_path / "cache/transcripts/new.txt"
    _old(old, now)
    new.parent.mkdir(parents=True)
    new.write_text("new", encoding="utf-8")
    found = sweep(tmp_path, RetentionConfig(1, 90), finished_at=now, dry_run=True)
    assert found == [old]
    assert old.exists()
    sweep(tmp_path, RetentionConfig(1, 90), finished_at=now)
    assert not old.exists()
    assert new.exists()


def test_raw_text_is_deleted_only_for_passed_leads(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    passed = tmp_path / "raw/passed.jsonl.gz"
    failed = tmp_path / "raw/failed.jsonl.gz"
    _old(passed, now)
    _old(failed, now)
    found = sweep(tmp_path, RetentionConfig(1, 90), finished_at=now, passed_leads={"passed"})
    assert found == [passed]
    assert passed in found and failed not in found


def test_outside_symlink_is_never_deleted(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    outside = tmp_path.parent / "outside-secret.txt"
    _old(outside, now)
    link = tmp_path / "raw/outside.txt"
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable")
    assert link not in sweep(tmp_path, RetentionConfig(1, 90), finished_at=now)
    assert outside.exists()


def test_spacy_model_present_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("bca.setup.spacy_model.importlib.util.find_spec", lambda name: object())
    called = False

    def run(*args: object, **kwargs: object) -> object:
        nonlocal called
        called = True
        return object()

    assert ensure_model(run=cast(Any, run)) is False
    assert not called


def test_spacy_model_install_uses_pinned_wheel_and_surfaces_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("bca.setup.spacy_model.importlib.util.find_spec", lambda name: None)
    commands: list[list[str]] = []

    class Result:
        returncode = 0

    def run(command: list[str], **kwargs: object) -> Result:
        commands.append(command)
        return Result()

    assert ensure_model(run=cast(Any, run)) is True
    assert commands == [
        [
            sys.executable,
            "-m",
            "uv",
            "pip",
            "install",
            MODEL_WHEEL_URL,
        ]
    ]

    class Failed:
        returncode = 1

    with pytest.raises(SpacyModelError, match="pinned spaCy model"):
        ensure_model(run=cast(Any, lambda *args, **kwargs: Failed()))
