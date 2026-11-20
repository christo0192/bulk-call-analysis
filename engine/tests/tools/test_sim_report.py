from __future__ import annotations

import importlib.util
from pathlib import Path

from tests.sim.harness import Scenario


def _report_module() -> object:
    path = Path(__file__).parents[3] / "scripts" / "sim_report.py"
    spec = importlib.util.spec_from_file_location("sim_report", path)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load sim_report")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_report_renders_rows_for_given_scenarios(tmp_path: Path) -> None:
    module = _report_module()
    output = tmp_path / "report.md"
    module.render([Scenario("small-happy"), Scenario("id-list-quality", source_ids=("0", "bad"))], output)  # type: ignore[attr-defined]
    lines = output.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("| scenario | final state |")
    assert sum(line.startswith("| ") for line in lines) == 4
