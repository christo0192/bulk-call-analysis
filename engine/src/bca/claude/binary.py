from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import urllib.request
from pathlib import Path
from typing import Any

EXPECTED_SIGNER = "Anthropic"


class BinaryVerificationError(RuntimeError):
    pass


class BinaryManager:
    def __init__(self, app_dir: Path, version: str) -> None:
        self.app_dir, self.version = app_dir, version
        self.destination = app_dir / "claude" / version

    def path(self) -> Path:
        path = self.destination / self._filename()
        if not path.is_file():
            raise BinaryVerificationError(f"verified Claude binary is missing: {path}")
        return path

    def _filename(self) -> str:
        return "claude.exe" if platform.system() == "Windows" else "claude"

    def _verify_signature(self, path: Path) -> None:
        if platform.system() == "Windows":
            command = [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                f"(Get-AuthenticodeSignature -LiteralPath '{path}').Status; "
                f"(Get-AuthenticodeSignature -LiteralPath '{path}').SignerCertificate.Subject",
            ]
            result = subprocess.run(command, capture_output=True, text=True, check=False)
            if (
                result.returncode != 0
                or "Valid" not in result.stdout
                or EXPECTED_SIGNER.lower() not in result.stdout.lower()
            ):
                raise BinaryVerificationError("Claude binary signature is not valid")
        else:
            result = subprocess.run(
                ["codesign", "--verify", "--deep", str(path)], capture_output=True, text=True, check=False
            )
            if result.returncode != 0:
                raise BinaryVerificationError("Claude binary signature is not valid")

    def _accept(self, path: Path, digest: str | None = None) -> Path:
        try:
            if digest and hashlib.sha256(path.read_bytes()).hexdigest().lower() != digest.lower():
                raise BinaryVerificationError("Claude binary SHA-256 mismatch")
            self._verify_signature(path)
        except (OSError, BinaryVerificationError, subprocess.SubprocessError):
            path.unlink(missing_ok=True)
            raise
        return path

    def install_from_local(self) -> Path:
        base = (
            Path(os.environ.get("USERPROFILE", str(Path.home())))
            if platform.system() == "Windows"
            else Path.home()
        )
        source = base / ".local" / "share" / "claude" / "versions" / self.version
        source = source if source.is_file() else source / self._filename()
        if not source.is_file():
            raise BinaryVerificationError(f"official Claude version is missing: {source}")
        self.destination.mkdir(parents=True, exist_ok=True)
        target = self.destination / self._filename()
        shutil.copy2(source, target)
        return self._accept(target)

    def install_from_download(self, url_template: str, manifest_url: str) -> Path:
        self.destination.mkdir(parents=True, exist_ok=True)
        target = self.destination / self._filename()
        url = url_template.format(
            version=self.version, platform=platform.system().lower(), arch=platform.machine().lower()
        )
        try:
            with urllib.request.urlopen(url) as response:
                target.write_bytes(response.read())
            with urllib.request.urlopen(manifest_url) as response:
                manifest: Any = json.loads(response.read().decode("utf-8"))
            digest = self._find_digest(manifest, target.name)
            return self._accept(target, digest)
        except Exception:
            target.unlink(missing_ok=True)
            raise

    def _find_digest(self, manifest: Any, filename: str) -> str:
        if isinstance(manifest, dict):
            for key, value in manifest.items():
                if (
                    isinstance(value, str)
                    and len(value) == 64
                    and (filename in str(key) or key in {"sha256", "sha256sum", "digest"})
                ):
                    return value
                try:
                    return self._find_digest(value, filename)
                except BinaryVerificationError:
                    pass
        if isinstance(manifest, list):
            for value in manifest:
                try:
                    return self._find_digest(value, filename)
                except BinaryVerificationError:
                    pass
        raise BinaryVerificationError("manifest has no SHA-256 for Claude binary")
