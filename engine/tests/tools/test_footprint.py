from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from bca.tools.footprint import main, private_working_set


def test_measures_current_process_tree_positive() -> None:
    assert private_working_set(os.getpid()) > 0


def test_sim_mode_prints_peak_and_steady(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    del tmp_path
    before = set(Path(tempfile.gettempdir()).glob("bca-footprint-*"))
    assert main(["--sim", "2"]) == 0
    output = capsys.readouterr().out.splitlines()
    assert len(output) == 2
    assert output[0].startswith("peak simulator tracemalloc:")
    assert output[1].startswith("steady process-tree private working set:")
    assert set(Path(tempfile.gettempdir()).glob("bca-footprint-*")) == before
