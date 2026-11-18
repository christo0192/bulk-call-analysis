from __future__ import annotations

import os
import platform as _platform
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppDirs:
    app_data: Path
    logs: Path
    cache: Path
    transcript_cache: Path
    projects: Path


def directories(
    *, env: Mapping[str, str] | None = None, home: Path | None = None, system: str | None = None
) -> AppDirs:
    values = os.environ if env is None else env
    home_path = Path.home() if home is None else home
    os_name = _platform.system() if system is None else system
    if os_name == "Windows":
        app = Path(values.get("LOCALAPPDATA", str(home_path / "AppData" / "Local"))) / "BulkCallAnalysis"
    elif os_name == "Darwin":
        app = home_path / "Library" / "Application Support" / "BulkCallAnalysis"
    else:
        app = Path(values.get("XDG_DATA_HOME", str(home_path / ".local" / "share"))) / "bulkcallanalysis"
    return AppDirs(
        app,
        app / "logs",
        app / "cache",
        app / "cache" / "transcripts",
        home_path / "Documents" / "Bulk Call Analysis",
    )


def is_cloud_synced(
    path: Path, *, env: Mapping[str, str] | None = None, home: Path | None = None, system: str | None = None
) -> str | None:
    values = os.environ if env is None else env
    home_path = Path.home() if home is None else home
    os_name = _platform.system() if system is None else system
    resolved = str(path.expanduser())
    if os_name == "Windows":
        lower = resolved.casefold()
        if (
            any(
                resolved.startswith(str(values[name]))
                for name in ("OneDrive", "OneDriveCommercial")
                if values.get(name)
            )
            or "\\onedrive" in lower
            or "/onedrive" in lower
        ):
            return "OneDrive"
    if os_name == "Darwin":
        try:
            relative = (
                path.expanduser()
                .resolve()
                .relative_to((home_path / "Library" / "Mobile Documents").resolve())
            )
            del relative
            return "iCloud"
        except ValueError:
            pass
        if any(part in {"Desktop", "Documents"} for part in path.expanduser().parts) and (
            "iCloud" in resolved or "Mobile Documents" in resolved
        ):
            return "iCloud"
    return None
