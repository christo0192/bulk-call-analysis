from __future__ import annotations

import hashlib
import json
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
    reseeded = store.resume_or_reseed("p", "v2", conversation_exists=False)
    assert reseeded.status == "new" and reseeded.session is not None


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


@pytest.mark.parametrize(
    ("payload", "ok"),
    [
        ({"loggedIn": True, "authMethod": "claude.ai"}, True),
        ({"loggedIn": True, "authMethod": "api_key"}, False),
        ({"loggedIn": True, "authMethod": "console"}, False),
        ({"loggedIn": False, "authMethod": None}, False),
    ],
)
def test_auth_status_rejects_non_subscription_logins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: dict[str, object], ok: bool
) -> None:
    class Completed:
        returncode = 0
        stdout = json.dumps(payload)

    monkeypatch.setattr("bca.claude.auth.subprocess.run", lambda *args, **kwargs: Completed())
    status = auth_status(tmp_path / "claude", ClaudeProfile(tmp_path / "config", tmp_path / "work"))
    assert status.ok is ok


def test_binary_signature_failure_deletes_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = BinaryManager(tmp_path / "app", "1.0")
    target = manager.destination / manager._filename()
    target.parent.mkdir(parents=True)
    target.write_bytes(b"binary")
    monkeypatch.setattr(
        manager,
        "_verify_signature",
        lambda path: (_ for _ in ()).throw(BinaryVerificationError("bad signature")),
    )
    with pytest.raises(BinaryVerificationError):
        manager._accept(target)
    assert not target.exists()


def test_download_install_uses_app_destination_and_mocked_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = BinaryManager(tmp_path / "app", "1.0")
    binary = b"downloaded"
    digest = hashlib.sha256(binary).hexdigest()

    class Response:
        def __init__(self, value: bytes) -> None:
            self.value = value

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return self.value

    monkeypatch.setattr(manager, "_verify_signature", lambda path: None)
    monkeypatch.setattr(
        "bca.claude.binary.urllib.request.urlopen",
        lambda url: Response(binary if "download" in str(url) else json.dumps({"sha256": digest}).encode()),
    )
    own_path = Path.home() / ".local" / "share" / "claude" / "versions" / "1.0"
    original = own_path.read_bytes() if own_path.is_file() else None
    installed = manager.install_from_download("https://download/{version}", "https://manifest")
    assert installed.read_bytes() == binary
    assert not own_path.exists() or own_path.read_bytes() == original
