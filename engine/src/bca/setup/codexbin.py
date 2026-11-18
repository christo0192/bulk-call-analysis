from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from collections.abc import Callable
from pathlib import Path

OFFICIAL_HOST = "github.com"
SIGNER = "OpenAI"
MAC_TEAM_REQUIREMENT = 'anchor apple generic and certificate leaf[subject.OU] = "2DC2775U2D"'
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
        self.destination = app_dir / "codex" / version

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
        return "codex-app-server-package-" + ASSET_SUFFIX[self.platform_name]

    def path(self) -> Path:
        target = self.destination / (
            "codex.exe" if self.platform_name and self.platform_name.startswith("win32") else "codex"
        )
        digest = target.with_suffix(target.suffix + ".sha256")
        if (
            not target.is_file()
            or not digest.is_file()
            or hashlib.sha256(target.read_bytes()).hexdigest() != digest.read_text(encoding="utf-8").strip()
        ):
            raise CodexVerificationError(f"verified Codex binary is missing or changed: {target}")
        return target

    def _verify_signature(self, path: Path) -> None:
        if platform.system() == "Windows":
            root = Path(os.environ.get("SYSTEMROOT", "C:\\Windows"))
            command = [
                str(root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"),
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$s=Get-AuthenticodeSignature -LiteralPath $args[0]; $s | Select-Object Status,@{N='Subject';E={$_.SignerCertificate.Subject}} | ConvertTo-Json -Compress",  # noqa: E501
                str(path.resolve()),
            ]
            result = subprocess.run(command, capture_output=True, text=True, check=False)
            try:
                data = json.loads(result.stdout)
            except json.JSONDecodeError:
                data = {}
            subject = data.get("Subject", "")
            signer_match = re.search(r"(?:^|,)\s*CN=([^,]+)", subject)
            if (
                result.returncode
                or data.get("Status") != "Valid"
                or not signer_match
                or signer_match.group(1) != SIGNER
            ):
                raise CodexVerificationError("Codex binary signature is not valid")
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
        self.destination.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=self.app_dir, suffix=".tar.gz", delete=False) as handle:
            temp = Path(handle.name)
            handle.write(archive)
        extraction_dir: Path | None = None
        try:
            with tarfile.open(temp, "r:gz") as bundle:
                member = next(
                    (item for item in bundle.getmembers() if Path(item.name).name in {"codex", "codex.exe"}),
                    None,
                )
                if member is None or not member.isfile():
                    raise CodexVerificationError("Codex archive has no binary")
                extraction_dir = Path(tempfile.mkdtemp(dir=self.app_dir))
                extracted = extraction_dir / Path(member.name).name
                source = bundle.extractfile(member)
                if source is None:
                    raise CodexVerificationError("Codex archive member cannot be read")
                extracted.write_bytes(source.read())
            self._verify_signature(extracted)
            target = self.destination / extracted.name
            os.replace(extracted, target)
            target.with_suffix(target.suffix + ".sha256").write_text(
                hashlib.sha256(target.read_bytes()).hexdigest() + "\n", encoding="utf-8"
            )
            return target
        finally:
            temp.unlink(missing_ok=True)
            if extraction_dir is not None:
                shutil.rmtree(extraction_dir, ignore_errors=True)
