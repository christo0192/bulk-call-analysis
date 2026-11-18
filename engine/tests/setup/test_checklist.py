import json
from pathlib import Path

import pytest

from bca.setup.checklist import MANUAL, NAMES, Checklist


def test_every_item_transitions_and_manual_items_need_explicit_call(tmp_path: Path) -> None:
    checklist = Checklist(tmp_path)
    result = checklist.run_checks({name: (lambda: (True, "checked")) for name in NAMES})
    assert all(item.status == ("manual_pending" if item.name in MANUAL else "ok") for item in result)
    checklist.mark_manual("training_off")
    checklist.mark_manual("extra_usage_off", done=False, detail="not confirmed")
    assert checklist.items[-2].status == "manual_done"
    assert checklist.items[-1].status == "manual_pending"
    with pytest.raises(ValueError):
        checklist.mark_manual("connector")


def test_failed_and_missing_probes_have_details(tmp_path: Path) -> None:
    checklist = Checklist(tmp_path)

    def broken() -> tuple[bool, str]:
        raise RuntimeError("synthetic failure")

    result = checklist.run_checks({"uv_python": broken})
    assert result[0].status == "failed"
    assert "synthetic failure" in result[0].detail
    assert result[1].status == "failed"
    assert result[1].detail == "probe not supplied"


def test_persistence_round_trip_is_atomic(tmp_path: Path) -> None:
    checklist = Checklist(tmp_path)
    checklist.run_checks({"uv_python": lambda: (False, "unavailable")})
    assert not list(tmp_path.glob(".checklist-*"))
    loaded = Checklist(tmp_path)
    assert loaded.items[0].status == "failed"
    assert loaded.items[0].detail == "unavailable"
    assert json.loads(loaded.path.read_text(encoding="utf-8"))[0]["name"] == "uv_python"
