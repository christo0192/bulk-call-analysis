from pathlib import Path

import pytest

from bca.setup.appdirs import directories, is_cloud_synced


@pytest.mark.parametrize(
    ("system", "env", "expected"),
    [
        ("Windows", {"LOCALAPPDATA": "local"}, Path("local/BulkCallAnalysis")),
        ("Darwin", {}, Path("home/Library/Application Support/BulkCallAnalysis")),
        ("Linux", {"XDG_DATA_HOME": "data"}, Path("data/bulkcallanalysis")),
    ],
)
def test_directories_are_injected_per_os(system: str, env: dict[str, str], expected: Path) -> None:
    result = directories(system=system, home=Path("home"), env=env)
    assert result.app_data == expected
    assert result.logs == expected / "logs"
    assert result.cache == expected / "cache"
    assert result.transcript_cache == expected / "cache" / "transcripts"
    assert result.projects == Path("home/Documents/Bulk Call Analysis")


def test_windows_cloud_detection_uses_env_roots() -> None:
    env = {"OneDrive": "C:/Users/test/OneDrive"}
    assert is_cloud_synced(Path("C:/Users/test/OneDrive/data"), system="Windows", env=env) == "OneDrive"
    assert is_cloud_synced(Path("D:/other/data"), system="Windows", env=env) is None


def test_windows_cloud_detection_uses_path_name() -> None:
    assert is_cloud_synced(Path("C:/OneDrive/data"), system="Windows", env={}) == "OneDrive"


def test_icloud_detection_and_unsynced_paths() -> None:
    home = Path("/user")
    assert (
        is_cloud_synced(
            home / "Library/Mobile Documents/com~apple~CloudDocs/x", system="Darwin", home=home, env={}
        )
        == "iCloud"
    )
    assert is_cloud_synced(home / "Documents/local.txt", system="Darwin", home=home, env={}) is None
    assert is_cloud_synced(home / "work/file", system="Linux", home=home, env={}) is None
