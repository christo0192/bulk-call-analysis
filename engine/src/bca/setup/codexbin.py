from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from collections.abc import Callable
from pathlib import Path

from bca.signing import verify_authenticode

OFFICIAL_HOST = "github.com"
SIGNER = "OpenAI OpCo, LLC"
MAC_TEAM_REQUIREMENT = 'anchor apple generic and certificate leaf[subject.OU] = "2DC432GLL2"'
ASSET_SUFFIX = {
    "win32-x64": "x86_64-pc-windows-msvc.tar.gz",
    "win32-arm64": "aarch64-pc-windows-msvc.tar.gz",
    "darwin-x64": "x86_64-apple-darwin.tar.gz",
    "darwin-arm64": "aarch64-apple-darwin.tar.gz",
}


class CodexVerificationError(RuntimeError):
    pass


class CodexBinaryManager:
    def __init__(
        self,
        app_dir: Path,
        version: str,
        *,
        platform_name: str | None = None,
        http_get: Callable[[str], bytes] | None = None,
    ) -> None:
        self.app_dir, self.version = app_dir, version
        self.platform_name = platform_name or self._current_platform()
        self.http_get = http_get or self._get
        self.destination = app_dir / "bin" / "codex" / version

    @staticmethod
    def _get(url: str) -> bytes:
        with urllib.request.urlopen(url) as response:
            return bytes(response.read())

    @staticmethod
    def _current_platform() -> str | None:
        machine = platform.machine().lower()
        arch = "arm64" if machine in {"arm64", "aarch64"} else "x64"
        return {"Windows": f"win32-{arch}", "Darwin": f"darwin-{arch}"}.get(platform.system())

    def _asset(self) -> str:
        if self.platform_name not in ASSET_SUFFIX:
            raise CodexVerificationError("unsupported Codex platform")
        return "codex-package-" + ASSET_SUFFIX[self.platform_name]

    def path(self) -> Path:
        target = self.destination / self._filename()
        digest = target.with_suffix(target.suffix + ".sha256")
        if (
            not target.is_file()
            or not digest.is_file()
            or hashlib.sha256(target.read_bytes()).hexdigest() != digest.read_text(encoding="utf-8").strip()
        ):
            raise CodexVerificationError(f"verified Codex binary is missing or changed: {target}")
        return target

    def _filename(self) -> str:
        return "codex.exe" if self.platform_name and self.platform_name.startswith("win32-") else "codex"

    def _verify_signature(self, path: Path) -> None:
        if platform.system() == "Windows":
            try:
                verify_authenticode(path, SIGNER)
            except Exception as exc:
                raise CodexVerificationError("Codex binary signature is not valid") from exc
        elif platform.system() == "Darwin":
            signature_result = subprocess.run(
                ["codesign", "--verify", "--strict", f"-R={MAC_TEAM_REQUIREMENT}", str(path)],
                capture_output=True,
                check=False,
            )
            if signature_result.returncode:
                raise CodexVerificationError("Codex binary signature is not valid")

    def install(self) -> Path:
        asset = self._asset()
        base = f"https://github.com/openai/codex/releases/download/rust-v{self.version}/"
        archive = self.http_get(base + asset)
        sums = self.http_get(base + "codex-package_SHA256SUMS").decode("utf-8")
        expected = next((line.split()[0] for line in sums.splitlines() if line.strip().endswith(asset)), None)
        if expected is None or hashlib.sha256(archive).hexdigest().lower() != expected.lower():
            raise CodexVerificationError("Codex package SHA-256 mismatch")
        temp: Path | None = None
        extraction_dir: Path | None = None
        binary_temp: Path | None = None
        try:
            self.app_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=self.app_dir, suffix=".tar.gz", delete=False) as handle:
                temp = Path(handle.name)
                handle.write(archive)
            with tarfile.open(temp, "r:gz") as bundle:
                filename = self._filename()
                member = next((item for item in bundle.getmembers() if item.name == f"bin/{filename}"), None)
                if member is None or not member.isfile():
                    raise CodexVerificationError("Codex archive has no binary")
                extraction_dir = Path(tempfile.mkdtemp(dir=self.app_dir))
                binary: bytes | None = None
                binary_digest: str | None = None
                for item in bundle.getmembers():
                    if not item.isfile() or not (
                        item.name.startswith("bin/")
                        or item.name.startswith("codex-resources/")
                        or item.name.startswith("codex-path/")
                    ):
                        continue
                    relative = Path(item.name)
                    if relative.is_absolute() or ".." in relative.parts:
                        raise CodexVerificationError("Codex archive contains an unsafe member")
                    output = extraction_dir / relative
                    output.parent.mkdir(parents=True, exist_ok=True)
                    source_handle = bundle.extractfile(item)
                    if source_handle is None:
                        raise CodexVerificationError("Codex archive member cannot be read")
                    contents = source_handle.read()
                    if item.name == member.name:
                        binary = contents
                        binary_digest = hashlib.sha256(contents).hexdigest()
                        continue
                    output.write_bytes(contents)
                if binary is None or binary_digest is None:
                    raise CodexVerificationError("Codex archive binary cannot be read")
                if self.platform_name and not self.platform_name.startswith("win32-"):
                    for candidate in extraction_dir.rglob("*"):
                        if candidate.is_file():
                            os.chmod(candidate, 0o755)
            for candidate in extraction_dir.rglob("*"):
                if candidate.is_file() and candidate.relative_to(extraction_dir).parts[0] in {
                    "bin",
                    "codex-path",
                }:
                    self._verify_signature(candidate)
            self.destination.mkdir(parents=True, exist_ok=True)
            target = self.destination / self._filename()
            for source_path in extraction_dir.rglob("*"):
                if source_path.is_file():
                    relative = source_path.relative_to(extraction_dir)
                    output = self.destination / relative
                    output.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(source_path, output)
            target = self.destination / self._filename()
            with tempfile.NamedTemporaryFile(
                dir=self.destination, prefix=f".{target.name}.", suffix=".tmp", delete=False
            ) as handle:
                binary_temp = Path(handle.name)
                handle.write(binary)
                handle.flush()
                os.fsync(handle.fileno())
            self._verify_signature(binary_temp)
            os.replace(binary_temp, target)
            binary_temp = None
            final_digest = hashlib.sha256(target.read_bytes()).hexdigest()
            if final_digest != binary_digest:
                raise CodexVerificationError("Codex binary changed during install")
            with target.with_suffix(target.suffix + ".sha256").open(
                "w", encoding="utf-8", newline="\n"
            ) as digest_file:
                digest_file.write(final_digest + "\n")
                digest_file.flush()
                os.fsync(digest_file.fileno())
            return target
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)
            if binary_temp is not None:
                binary_temp.unlink(missing_ok=True)
            if extraction_dir is not None:
                shutil.rmtree(extraction_dir, ignore_errors=True)
