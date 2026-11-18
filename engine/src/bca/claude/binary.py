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
from urllib.parse import urlparse

from bca.signing import verify_authenticode

EXPECTED_SIGNER = "Anthropic PBC"
MAC_TEAM_ID = "Q6L2SF6YDW"
OFFICIAL_HOST = "downloads.claude.ai"


class BinaryVerificationError(RuntimeError):
    pass


class BinaryManager:
    def __init__(
        self,
        app_dir: Path,
        version: str,
        versions_dir: Path | None = None,
        platform_name: str | None = None,
    ) -> None:
        self.app_dir, self.version = app_dir, version
        self.destination = app_dir / "claude" / version
        self.versions_dir = versions_dir
        self._platform_name_override = platform_name

    def path(self) -> Path:
        path = self.destination / self._filename()
        if not path.is_file():
            raise BinaryVerificationError(f"verified Claude binary is missing: {path}")
        digest_path = path.with_suffix(path.suffix + ".sha256")
        if (
            not digest_path.is_file()
            or hashlib.sha256(path.read_bytes()).hexdigest() != digest_path.read_text().strip()
        ):
            raise BinaryVerificationError(f"Claude binary digest changed: {path}")
        return path

    def _filename(self) -> str:
        platform_name = self._platform_name()
        return "claude.exe" if platform_name and platform_name.startswith("win32-") else "claude"

    def _verify_signature(self, path: Path) -> None:
        if platform.system() == "Windows":
            try:
                verify_authenticode(path, EXPECTED_SIGNER)
            except Exception as exc:
                raise BinaryVerificationError("Claude binary signature is not valid") from exc
        else:
            result = subprocess.run(
                [
                    "codesign",
                    "--verify",
                    "--strict",
                    f'-R=anchor apple generic and certificate leaf[subject.OU] = "{MAC_TEAM_ID}"',
                    str(path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                raise BinaryVerificationError("Claude binary signature is not valid")

    def _accept(self, path: Path, digest: str | None = None) -> Path:
        try:
            if digest and hashlib.sha256(path.read_bytes()).hexdigest().lower() != digest.lower():
                raise BinaryVerificationError("Claude binary SHA-256 mismatch")
            self._verify_signature(path)
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            path.with_suffix(path.suffix + ".sha256").write_text(actual + "\n", encoding="utf-8")
        except (OSError, BinaryVerificationError, subprocess.SubprocessError):
            path.unlink(missing_ok=True)
            raise
        return path

    def install_from_local(self) -> Path:
        base = self.versions_dir or (
            Path(os.environ.get("USERPROFILE", str(Path.home())))
            if platform.system() == "Windows"
            else Path.home()
        )
        source = (
            base / self.version
            if self.versions_dir
            else base / ".local" / "share" / "claude" / "versions" / self.version
        )
        source = source if source.is_file() else source / self._filename()
        if not source.is_file():
            raise BinaryVerificationError(f"official Claude version is missing: {source}")
        self.destination.mkdir(parents=True, exist_ok=True)
        target = self.destination / self._filename()
        temp = target.with_name(target.name + ".tmp")
        shutil.copy2(source, temp)
        if not (self._platform_name() or "").startswith("win32-"):
            os.chmod(temp, 0o755)
        accepted = self._accept(temp)
        os.replace(accepted, target)
        os.replace(temp.with_suffix(temp.suffix + ".sha256"), target.with_suffix(target.suffix + ".sha256"))
        return target

    def install_from_download(self, url_template: str, manifest_url: str) -> Path:
        self.destination.mkdir(parents=True, exist_ok=True)
        target = self.destination / self._filename()
        platform_name = self._platform_name()
        if platform_name is None:
            raise BinaryVerificationError("unsupported Claude platform")
        url = url_template.format(
            version=self.version, platform=platform_name, arch=platform_name.split("-", 1)[1]
        )
        for candidate in (url, manifest_url):
            parsed = urlparse(candidate)
            if parsed.scheme != "https" or parsed.hostname != OFFICIAL_HOST:
                raise BinaryVerificationError("Claude download URL is not the official HTTPS host")
        try:
            temp = target.with_name(target.name + ".tmp")
            with urllib.request.urlopen(url) as response:
                temp.write_bytes(response.read())
            if not platform_name.startswith("win32-"):
                os.chmod(temp, 0o755)
            with urllib.request.urlopen(manifest_url) as response:
                manifest: Any = json.loads(response.read().decode("utf-8"))
            digest = self._find_digest(manifest, platform_name)
            platform_manifest = manifest["platforms"][platform_name]
            expected_size = platform_manifest.get("size") if isinstance(platform_manifest, dict) else None
            if expected_size is not None and expected_size != temp.stat().st_size:
                raise BinaryVerificationError("Claude binary size mismatch")
            accepted = self._accept(temp, digest)
            os.replace(accepted, target)
            os.replace(
                temp.with_suffix(temp.suffix + ".sha256"), target.with_suffix(target.suffix + ".sha256")
            )
            return target
        except Exception:
            target.with_name(target.name + ".tmp").unlink(missing_ok=True)
            raise

    def _find_digest(self, manifest: Any, platform_name: str) -> str:
        try:
            digest = manifest["platforms"][platform_name]["checksum"]
        except (KeyError, TypeError):
            raise BinaryVerificationError("manifest has no platform checksum") from None
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
        ):
            raise BinaryVerificationError("manifest checksum is not lowercase SHA-256")
        return digest

    def _platform_name(self) -> str | None:
        return self._platform_name_override or self._current_platform_name()

    @staticmethod
    def _current_platform_name() -> str | None:
        system = platform.system()
        machine = platform.machine().lower()
        arch = "arm64" if machine in {"arm64", "aarch64"} else "x64"
        return {"Windows": f"win32-{arch}", "Darwin": f"darwin-{arch}", "Linux": f"linux-{arch}"}.get(system)
