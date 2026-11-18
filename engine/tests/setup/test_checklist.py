from pathlib import Path

from bca.setup.checklist import Checklist


def test_checks_and_manual_items(tmp_path: Path) -> None:
    checklist = Checklist(tmp_path)
    result = checklist.run_checks({"uv_python": lambda: (True, "available")})
    assert result[0].status == "ok"
    assert result[-1].status == "manual_pending"
    checklist.mark_manual("training_off")
    assert checklist.items[9].status == "manual_done"
