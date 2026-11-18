from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from bca.claude.auth import auth_status
from bca.claude.binary import BinaryManager, BinaryVerificationError
from bca.claude.profile import ClaudeProfile
from bca.claude.sessions import SessionStore


def test_session_store_resume_and_playbook_choice(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.json")
    assert store.get("p", "v1").status == "new"
    session = store.new("p", "v1")
    assert store.get("p", "v1").session == session
    assert store.get("p", "v2").status == "needs_choice"
    assert store.reseed("p", "v2") != session


def test_binary_local_copy_and_digest_rejection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    version = "1.0.0"
    source = tmp_path / "home" / ".local" / "share" / "claude" / "versions"
    source.mkdir(parents=True)
    source_file = source / version
    source_file.write_bytes(b"official")
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    manager = BinaryManager(tmp_path / "app", version)
    monkeypatch.setattr(manager, "_verify_signature", lambda path: None)
    assert manager.install_from_local().read_bytes() == b"official"
    assert hashlib.sha256(b"wrong").hexdigest() != hashlib.sha256(b"official").hexdigest()
    target = manager.destination / "claude.exe"
    target.write_bytes(b"wrong")
    try:
        manager._accept(target, hashlib.sha256(b"official").hexdigest())
    except BinaryVerificationError:
        assert not target.exists()
    else:
        raise AssertionError("digest mismatch was accepted")


def test_auth_status_accepts_subscription_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Completed:
        returncode = 0
        stdout = '{"loggedIn": true, "authMethod": "claude.ai", "configDirectory": "isolated"}'

    monkeypatch.setattr("bca.claude.auth.subprocess.run", lambda *args, **kwargs: Completed())
    status = auth_status(tmp_path / "claude", ClaudeProfile(tmp_path / "config", tmp_path / "work"))
    assert status.ok
