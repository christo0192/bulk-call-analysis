from __future__ import annotations

import json
import os
import platform
import subprocess
from pathlib import Path


class AuthenticodeError(RuntimeError):
    pass


def verify_authenticode(path: Path, expected_signer: str) -> None:
    root = Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
    powershell = root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    command = (
        "$p=[Environment]::GetEnvironmentVariable('BCA_SIGNING_PATH'); "
        "$s=Get-AuthenticodeSignature -LiteralPath $p; "
        "$signer=if ($null -eq $s.SignerCertificate) { $null } "
        "else { $s.SignerCertificate.GetNameInfo('SimpleName',$false) }; "
        "[pscustomobject]@{Status=$s.Status.ToString();Signer=$signer} "
        "| ConvertTo-Json -Compress"
    )
    child_env = os.environ.copy()
    child_env["BCA_SIGNING_PATH"] = str(path.resolve())
    result = subprocess.run(
        [str(powershell), "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        check=False,
        env=child_env,
    )
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        data = {}
    if result.returncode != 0 or data.get("Status") != "Valid" or data.get("Signer") != expected_signer:
        raise AuthenticodeError("Authenticode signature is not valid")


def verify_windows(path: Path, expected_signer: str) -> None:
    if platform.system() == "Windows":
        verify_authenticode(path, expected_signer)
