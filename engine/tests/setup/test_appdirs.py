from pathlib import Path

from bca.setup.appdirs import directories, is_cloud_synced


def test_windows_directories_are_injected() -> None:
    result = directories(system="Windows", home=Path("H:/home"), env={"LOCALAPPDATA": "L:/local"})
    assert result.app_data == Path("L:/local/BulkCallAnalysis")
    assert result.projects == Path("H:/home/Documents/Bulk Call Analysis")


def test_cloud_sync_detection() -> None:
    assert is_cloud_synced(Path("C:/OneDrive/data"), system="Windows", env={}) == "OneDrive"
    assert (
        is_cloud_synced(Path("/u/Library/Mobile Documents/x"), system="Darwin", home=Path("/u"), env={})
        == "iCloud"
    )
