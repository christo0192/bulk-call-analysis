import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from bca.setup.codexbin import ASSET_SUFFIX, CodexBinaryManager, CodexVerificationError


def _archive(name: str = "codex") -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as bundle:
        data = b"synthetic codex binary"
        info = tarfile.TarInfo("bin/" + name)
        info.size = len(data)
        bundle.addfile(info, io.BytesIO(data))
    return output.getvalue()


@pytest.mark.parametrize("platform_name", ASSET_SUFFIX)
def test_platform_asset_mapping(platform_name: str) -> None:
    assert (
        CodexBinaryManager(Path("app"), "1", platform_name=platform_name)
        ._asset()
        .endswith(ASSET_SUFFIX[platform_name])
    )


def test_download_sha_signature_extract_replace_and_no_path_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _archive("codex.exe")
    asset = "codex-package-" + ASSET_SUFFIX["win32-x64"]
    calls: list[str] = []

    def http(url: str) -> bytes:
        calls.append(url)
        return (
            archive
            if url.endswith(asset)
            else (hashlib.sha256(archive).hexdigest() + "  " + asset + "\n").encode()
        )

    manager = CodexBinaryManager(tmp_path, "1", platform_name="win32-x64", http_get=http)
    monkeypatch.setattr("bca.setup.codexbin.platform.system", lambda: "Windows")
    monkeypatch.setattr("bca.setup.codexbin.os.environ", {"SYSTEMROOT": str(tmp_path / "system")})
    seen: list[Path] = []

    def verify(path: Path) -> None:
        seen.append(path)

    monkeypatch.setattr(manager, "_verify_signature", verify)
    target = manager.install()
    assert target == manager.destination / "codex.exe"
    assert target.read_bytes() == b"synthetic codex binary"
    assert (
        target.with_suffix(target.suffix + ".sha256").read_text(encoding="utf-8").strip()
        == hashlib.sha256(target.read_bytes()).hexdigest()
    )
    assert seen and seen[0] != target
    assert len(calls) == 2
    assert not list(tmp_path.glob("*.tar.gz"))
    assert not list(tmp_path.glob("tmp*"))


def test_sha_mismatch_rejected_and_temp_archive_deleted(tmp_path: Path) -> None:
    archive = _archive()
    manager = CodexBinaryManager(
        tmp_path,
        "1",
        platform_name="darwin-x64",
        http_get=lambda url: archive if url.endswith("tar.gz") else b"0 wrong",
    )
    with pytest.raises(CodexVerificationError, match="SHA-256"):
        manager.install()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "payload",
    [
        {"Status": "Invalid", "Subject": "CN=OpenAI"},
        {"Status": "Valid", "Subject": "CN=OpenAI Spoof"},
    ],
)
def test_signature_failure_and_spoofed_signer_are_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: dict[str, str]
) -> None:
    monkeypatch.setattr("bca.setup.codexbin.platform.system", lambda: "Windows")
    monkeypatch.setattr("bca.setup.codexbin.os.environ", {"SYSTEMROOT": str(tmp_path)})
    monkeypatch.setattr(
        "bca.setup.codexbin.subprocess.run",
        lambda *args, **kwargs: type(
            "R",
            (),
            {"returncode": 0, "stdout": json.dumps(payload)},
        )(),
    )
    with pytest.raises(CodexVerificationError):
        CodexBinaryManager(tmp_path, "1", platform_name="win32-x64")._verify_signature(tmp_path / "codex.exe")


def test_authenticode_accepts_real_powershell_shape_and_exact_signer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("bca.setup.codexbin.platform.system", lambda: "Windows")
    monkeypatch.setattr("bca.setup.codexbin.os.environ", {"SYSTEMROOT": str(tmp_path)})
    monkeypatch.setattr(
        "bca.setup.codexbin.subprocess.run",
        lambda *args, **kwargs: type(
            "R", (), {"returncode": 0, "stdout": '{"Status":"Valid","Signer":"OpenAI OpCo, LLC"}'}
        )(),
    )
    CodexBinaryManager(tmp_path, "1", platform_name="win32-x64")._verify_signature(tmp_path / "codex.exe")


def test_rehash_detects_swapped_file_and_path_does_not_use_path_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = CodexBinaryManager(tmp_path, "1", platform_name="darwin-x64")
    target = manager.destination / "codex"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"original")
    target.with_suffix(".sha256").write_text(hashlib.sha256(b"original").hexdigest(), encoding="utf-8")
    monkeypatch.setenv("PATH", "should-not-matter")
    assert manager.path() == target
    target.write_bytes(b"swapped")
    with pytest.raises(CodexVerificationError):
        manager.path()
