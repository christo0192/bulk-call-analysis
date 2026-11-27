"""The app runs its own verified copy of one pinned Claude Code version. Synthetic files only.

Claude Code updates itself. The app launched whichever `claude` was on PATH, so after an update every
chat failed: the turn guard accepts only the version its checks were verified against. The app now
keeps its own copy of the pinned version (a hard link to the official install, so it costs no disk
space), and Claude's built-in plugins, new in that version, are switched off in its settings.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from bca.claude import binary
from bca.claude.binary import EXPECTED_SIGNER, BinaryManager, BinaryVerificationError
from bca.claude.profile import ClaudeProfile
from bca.claude.stream import Event, check_init
from bca.claude.version import BUILTIN_PLUGINS, PINNED_CLAUDE_VERSION
from tests.api.test_production import make_backend


def _official(home: Path, version: str = PINNED_CLAUDE_VERSION, data: bytes = b"official program") -> Path:
    """Where the official installer keeps a version: ~/.local/share/claude/versions/<version>."""
    path = home / ".local" / "share" / "claude" / "versions" / version
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "home"
    folder.mkdir()
    monkeypatch.setenv("USERPROFILE", str(folder))
    monkeypatch.setenv("HOME", str(folder))
    monkeypatch.delenv("BCA_CLAUDE_BIN", raising=False)
    monkeypatch.delenv("BCA_TEST_FAKES", raising=False)
    monkeypatch.setattr(BinaryManager, "_verify_signature", lambda self, path: None)
    binary._verified.clear()
    return folder


def _init(version: str, plugins: list[dict[str, str]] | None = None) -> Event:
    return Event(
        "system",
        {
            "type": "system",
            "subtype": "init",
            "apiKeySource": "none",
            "model": "claude-opus-5-5",
            "permissionMode": "dontAsk",
            "claude_code_version": version,
            "tools": [],
            "mcp_servers": [{"name": "bq", "status": "connected"}, {"name": "app", "status": "connected"}],
            "skills": [],
            "plugins": plugins or [],
        },
    )


def test_the_guard_accepts_only_the_pinned_version() -> None:
    assert check_init(_init(PINNED_CLAUDE_VERSION), "claude-opus-5-5") == []
    assert "unexpected Claude Code version" in check_init(_init("2.1.327"), "claude-opus-5-5")


def test_the_builtin_plugins_are_switched_off_and_the_guard_still_rejects_loaded_ones(tmp_path: Path) -> None:
    settings = json.loads(ClaudeProfile(tmp_path / "config", tmp_path / "work").write_settings().read_text())
    assert settings["enabledPlugins"] == dict.fromkeys(BUILTIN_PLUGINS, False)
    # What 2.1.332 reports when they are not switched off: the guard still refuses the turn.
    loaded = [{"name": "cc-plugin-privacy", "path": "builtin", "source": "cc-plugin-privacy@builtin"}]
    assert "plugins were loaded" in check_init(_init(PINNED_CLAUDE_VERSION, loaded), "claude-opus-5-5")


def test_the_signer_name_is_the_one_windows_reports() -> None:
    # Get-AuthenticodeSignature reports "Anthropic, PBC" for the official program; the old
    # "Anthropic PBC" could never match, so no copy would ever have been accepted.
    assert EXPECTED_SIGNER == "Anthropic, PBC"


@pytest.mark.skipif(sys.platform != "win32", reason="Authenticode is Windows only")
def test_the_installed_official_program_passes_the_signature_check() -> None:
    from bca.signing import verify_authenticode

    official = (
        Path(os.environ["USERPROFILE"]) / ".local" / "share" / "claude" / "versions" / PINNED_CLAUDE_VERSION
    )
    if not official.is_file():
        pytest.skip("the pinned Claude Code version is not installed on this machine")
    verify_authenticode(official, EXPECTED_SIGNER)


def test_the_copy_is_a_hard_link_that_outlives_the_official_file(home: Path, tmp_path: Path) -> None:
    official = _official(home)
    manager = BinaryManager(tmp_path / "app", PINNED_CLAUDE_VERSION)
    installed = manager.install_from_local()
    assert os.path.samefile(installed, official)
    # Claude's own updater later removes this version; the app's program is still there.
    official.unlink()
    assert manager.path().read_bytes() == b"official program"


def test_a_copy_is_made_only_when_linking_fails_and_the_disk_has_room(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _official(home)

    def no_link(source: object, target: object) -> None:
        raise OSError("synthetic: links not supported here")

    monkeypatch.setattr(os, "link", no_link)
    real_usage = shutil.disk_usage
    monkeypatch.setattr(shutil, "disk_usage", lambda path: real_usage(path)._replace(free=1024))
    manager = BinaryManager(tmp_path / "app", PINNED_CLAUDE_VERSION)
    with pytest.raises(BinaryVerificationError, match="not enough disk space"):
        manager.install_from_local()
    assert not (manager.destination / manager._filename()).exists()
    monkeypatch.setattr(shutil, "disk_usage", real_usage)
    installed = manager.install_from_local()
    assert installed.read_bytes() == b"official program"
    assert not os.path.samefile(
        installed, home / ".local" / "share" / "claude" / "versions" / PINNED_CLAUDE_VERSION
    )


def test_the_program_is_hashed_once_per_change(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _official(home)
    manager = BinaryManager(tmp_path / "app", PINNED_CLAUDE_VERSION)
    installed = manager.install_from_local()
    hashed: list[Path] = []
    real = binary._sha256

    def counting(path: Path) -> str:
        hashed.append(path)
        return real(path)

    monkeypatch.setattr(binary, "_sha256", counting)
    manager.path()
    manager.path()
    assert hashed == []
    # A changed program is hashed again, and rejected.
    stat = installed.stat()
    installed.write_bytes(b"something else entirely")
    os.utime(installed, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    with pytest.raises(BinaryVerificationError, match="digest changed"):
        manager.path()
    assert hashed == [installed]


def test_a_program_failing_the_signature_check_is_not_kept(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    official = _official(home)

    def refuse(self: BinaryManager, path: Path) -> None:
        raise BinaryVerificationError("Claude binary signature is not valid")

    monkeypatch.setattr(BinaryManager, "_verify_signature", refuse)
    manager = BinaryManager(tmp_path / "app", PINNED_CLAUDE_VERSION)
    with pytest.raises(BinaryVerificationError):
        manager.install_from_local()
    assert not any(manager.destination.iterdir())
    assert official.read_bytes() == b"official program"


def _claude_row(backend: Any) -> dict[str, Any]:
    checks = cast(dict[str, Any], asyncio.run(backend.call("setup/runChecks", {})))
    return next(item for item in checks["items"] if item["name"] == "claude_copy")


def test_the_engine_launches_its_own_copy_and_the_row_says_so(home: Path, tmp_path: Path) -> None:
    official = _official(home)
    backend = make_backend(tmp_path / "data")
    program = backend._claude_binary()
    assert program == (tmp_path / "data" / "programs" / "claude" / PINNED_CLAUDE_VERSION / program.name)
    assert os.path.samefile(program, official)
    row = _claude_row(backend)
    assert row["status"] == "ok"
    assert row["detail"] == f"Claude Code {PINNED_CLAUDE_VERSION} ready."


def test_without_the_pinned_version_the_row_says_how_to_install_it(home: Path, tmp_path: Path) -> None:
    _official(home, version="2.1.999")
    backend = make_backend(tmp_path / "data")
    program = backend._claude_binary()
    assert program.is_absolute() and not program.exists()
    row = _claude_row(backend)
    assert row["status"] == "failed"
    assert f"claude install {PINNED_CLAUDE_VERSION}" in row["detail"]


def test_a_configured_program_is_used_as_is(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = tmp_path / "tools" / ("claude.exe" if sys.platform == "win32" else "claude")
    other.parent.mkdir(parents=True)
    other.write_bytes(b"development build")
    monkeypatch.setenv("BCA_CLAUDE_BIN", str(other))
    backend = make_backend(tmp_path / "data")
    assert backend._claude_binary() == other.absolute()
    assert not (tmp_path / "data" / "programs").exists()


def _unusable(program: Path) -> bool:
    return program.is_absolute() and not program.exists()


def test_a_planted_program_without_a_digest_is_never_returned(home: Path, tmp_path: Path) -> None:
    # Nothing official installed, and a file planted where the app keeps its copy.
    backend = make_backend(tmp_path / "data")
    planted = tmp_path / "data" / "programs" / "claude" / PINNED_CLAUDE_VERSION / "claude.exe"
    planted.parent.mkdir(parents=True)
    planted.write_bytes(b"planted")
    program, usable, _detail = backend._claude_program()
    assert not usable and _unusable(program) and program != planted
    row = _claude_row(backend)
    assert row["status"] == "failed"


def test_a_copy_changed_after_the_official_file_went_is_never_returned(home: Path, tmp_path: Path) -> None:
    official = _official(home)
    backend = make_backend(tmp_path / "data")
    program, usable, _detail = backend._claude_program()
    assert usable
    official.unlink()
    stat = program.stat()
    program.write_bytes(b"changed afterwards")
    os.utime(program, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    again, usable, detail = backend._claude_program()
    assert not usable and _unusable(again)
    assert f"claude install {PINNED_CLAUDE_VERSION}" in detail


def test_an_official_file_failing_the_signature_check_is_never_returned(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    official = _official(home)
    backend = make_backend(tmp_path / "data")
    _program, usable, _detail = backend._claude_program()
    assert usable
    # The official file is rewritten in place (the app's copy is the same file) with bytes that
    # fail the signature check: the changed copy must not be launched.
    stat = official.stat()
    official.write_bytes(b"unsigned bytes")
    os.utime(official, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

    def refuse(self: BinaryManager, path: Path) -> None:
        raise BinaryVerificationError("Claude binary signature is not valid")

    monkeypatch.setattr(BinaryManager, "_verify_signature", refuse)
    again, usable, detail = backend._claude_program()
    assert not usable and _unusable(again)
    assert "did not pass the app's checks" in detail
    assert _claude_row(backend)["status"] == "failed"


def test_a_failed_attempt_is_not_repeated_at_once(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[int] = []
    real = BinaryManager.install_from_local

    def counting(self: BinaryManager) -> Path:
        attempts.append(1)
        return real(self)

    monkeypatch.setattr(BinaryManager, "install_from_local", counting)
    backend = make_backend(tmp_path / "data")
    for _ in range(3):
        assert not backend._claude_program()[1]
    assert len(attempts) == 1
    # After the pause, and once the version is installed, the next call tries again and succeeds.
    _official(home)
    monkeypatch.setattr(backend, "_CLAUDE_RETRY_S", 0.0)
    assert backend._claude_program()[1]
    assert len(attempts) == 2


def test_copies_of_earlier_pinned_versions_are_removed(home: Path, tmp_path: Path) -> None:
    _official(home)
    old = tmp_path / "app" / "claude" / "2.1.327"
    old.mkdir(parents=True)
    (old / "claude.exe").write_bytes(b"old pinned copy")
    BinaryManager(tmp_path / "app", PINNED_CLAUDE_VERSION).install_from_local()
    assert [folder.name for folder in (tmp_path / "app" / "claude").iterdir()] == [PINNED_CLAUDE_VERSION]


def test_a_file_changed_during_its_check_is_not_recorded_as_verified(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    official = _official(home)

    def changes_meanwhile(self: BinaryManager, path: Path) -> None:
        # The signature check takes seconds; the file is rewritten in place meanwhile.
        stat = official.stat()
        official.write_bytes(b"rewritten during the check")
        os.utime(official, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

    monkeypatch.setattr(BinaryManager, "_verify_signature", changes_meanwhile)
    manager = BinaryManager(tmp_path / "app", PINNED_CLAUDE_VERSION)
    with pytest.raises(BinaryVerificationError, match="changed while it was being checked"):
        manager.install_from_local()
    with pytest.raises(BinaryVerificationError, match="digest changed"):
        manager.path()


def test_no_test_runs_the_installed_claude_by_default() -> None:
    # The autouse fixture in tests/conftest.py points every test at a program that does not exist.
    configured = Path(os.environ["BCA_CLAUDE_BIN"])
    assert configured.name == "claude.exe" and not configured.exists()


def test_a_bad_copy_is_not_hashed_again_while_nothing_changed(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    official = _official(home)
    backend = make_backend(tmp_path / "data")
    program, usable, _detail = backend._claude_program()
    assert usable
    official.unlink()
    stat = program.stat()
    program.write_bytes(b"changed afterwards")
    os.utime(program, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    assert not backend._claude_program()[1]
    hashed: list[Path] = []
    real = binary._sha256

    def counting(path: Path) -> str:
        hashed.append(path)
        return real(path)

    monkeypatch.setattr(binary, "_sha256", counting)
    for _ in range(5):
        assert not backend._claude_program()[1]
    assert hashed == []


def test_installing_the_version_is_picked_up_at_once(home: Path, tmp_path: Path) -> None:
    backend = make_backend(tmp_path / "data")
    assert not backend._claude_program()[1]
    _official(home)  # the user runs `claude install`, well inside the pause
    program, usable, detail = backend._claude_program()
    assert usable and program.is_file()
    assert detail == f"Claude Code {PINNED_CLAUDE_VERSION} ready."


def test_each_kind_of_failure_says_what_to_do(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _official(home)

    def no_link(source: object, target: object) -> None:
        raise OSError("synthetic: links not supported here")

    monkeypatch.setattr(os, "link", no_link)
    real_usage = shutil.disk_usage
    monkeypatch.setattr(shutil, "disk_usage", lambda path: real_usage(path)._replace(free=1024))
    _program, usable, detail = make_backend(tmp_path / "a")._claude_program()
    assert not usable and "not enough free disk space" in detail and "Reinstall" not in detail

    def refuse_copy(source: object, target: object) -> None:
        raise PermissionError("synthetic: access denied")

    monkeypatch.setattr(shutil, "disk_usage", real_usage)
    monkeypatch.setattr(shutil, "copy2", refuse_copy)
    _program, usable, detail = make_backend(tmp_path / "b")._claude_program()
    assert not usable and "could not make its copy" in detail and "access denied" in detail


def test_the_checklist_resolves_the_program_once_for_both_claude_rows(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _official(home)
    backend = make_backend(tmp_path / "data")
    calls: list[int] = []
    real = backend._claude_program

    def counting() -> tuple[Path, bool, str]:
        calls.append(1)
        return real()

    monkeypatch.setattr(backend, "_claude_program", counting)
    monkeypatch.setattr("bca.api.production.claude_auth_status", lambda program, profile: None)
    row = _claude_row(backend)
    assert row["status"] == "ok" and calls == [1]
