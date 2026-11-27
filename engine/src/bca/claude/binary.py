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

# The signer's name as Windows reports it (Get-AuthenticodeSignature, SimpleName), comma included.
EXPECTED_SIGNER = "Anthropic, PBC"
MAC_TEAM_ID = "Q6L2SF6YDW"
OFFICIAL_HOST = "downloads.claude.ai"


class BinaryVerificationError(RuntimeError):
    pass


class NotEnoughSpaceError(BinaryVerificationError):
    """The program could not be linked, and the disk has no room for a copy."""


# Room left on the disk when the program has to be copied rather than linked.
_COPY_MARGIN = 200 * 1024**2
# Programs whose digest was checked in this process, by (path, size, mtime): the program is about
# 400 MB, and hashing it before every Claude turn would add a second or more to each.
_verified: dict[tuple[str, int, int], str] = {}


def _sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _stamp(path: Path) -> tuple[str, int, int]:
    stat = path.stat()
    return (str(path), stat.st_size, stat.st_mtime_ns)


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
        if not digest_path.is_file():
            raise BinaryVerificationError(f"Claude binary digest changed: {path}")
        expected = digest_path.read_text().strip()
        stamp = _stamp(path)
        if _verified.get(stamp) != expected:
            if _sha256(path) != expected:
                raise BinaryVerificationError(f"Claude binary digest changed: {path}")
            # Cached only if the file did not change while it was hashed.
            if _stamp(path) == stamp:
                _verified[stamp] = expected
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
            actual = _sha256(path)
            if digest and actual != digest.lower():
                raise BinaryVerificationError("Claude binary SHA-256 mismatch")
            self._verify_signature(path)
            path.with_suffix(path.suffix + ".sha256").write_text(actual + "\n", encoding="utf-8")
        except (OSError, BinaryVerificationError, subprocess.SubprocessError):
            path.unlink(missing_ok=True)
            raise
        return path

    def official_path(self) -> Path:
        """Where the official installer keeps this version (~/.local/share/claude/versions)."""
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
        return source if source.is_file() else source / self._filename()

    def install_from_local(self) -> Path:
        source = self.official_path()
        if not source.is_file():
            raise BinaryVerificationError(f"official Claude version is missing: {source}")
        self.destination.mkdir(parents=True, exist_ok=True)
        target = self.destination / self._filename()
        temp = target.with_name(target.name + ".tmp")
        temp.unlink(missing_ok=True)
        try:
            # A hard link: no second copy of the program on disk, and the app keeps its program
            # after Claude's own updater removes this version from its folder.
            os.link(source, temp)
        except OSError:
            needed = source.stat().st_size + _COPY_MARGIN
            free = shutil.disk_usage(self.destination).free
            if free < needed:
                raise NotEnoughSpaceError(
                    f"not enough disk space to copy Claude Code {self.version}: "
                    f"{needed // 1024**2} MB needed, {free // 1024**2} MB free"
                ) from None
            shutil.copy2(source, temp)
            if not (self._platform_name() or "").startswith("win32-"):
                os.chmod(temp, 0o755)
        # Taken before the file is hashed: the signature check takes seconds, and a file changed
        # meanwhile must not be recorded as verified.
        before = _stamp(temp)[1:]
        accepted = self._accept(temp)
        digest = temp.with_suffix(temp.suffix + ".sha256")
        os.replace(accepted, target)
        os.replace(digest, target.with_suffix(target.suffix + ".sha256"))
        if _stamp(target)[1:] != before:
            raise BinaryVerificationError("Claude Code changed while it was being checked")
        _verified[_stamp(target)] = target.with_suffix(target.suffix + ".sha256").read_text().strip()
        self._remove_other_versions()
        return target

    def _remove_other_versions(self) -> None:
        """Copies of earlier pinned versions are never run again; each is about 400 MB once the
        official file is gone, so they are removed (one still in use is left for next time)."""
        for folder in self.destination.parent.iterdir():
            if folder.is_dir() and folder.name != self.version:
                shutil.rmtree(folder, ignore_errors=True)

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
