from __future__ import annotations

import sys
from tempfile import TemporaryDirectory
from pathlib import Path
from time import perf_counter
from typing import Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))

from tests.sim.harness import Scenario, assert_invariants, run_scenario
from tests.sim.test_scenarios import SCENARIOS


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_report(scenarios: Iterable[Scenario], output: Path, root: Path | None = None) -> None:
    base = root or output.parent
    rows: list[str] = []
    for scenario in scenarios:
        started = perf_counter()
        result = None
        with TemporaryDirectory(prefix=f"sim-report-{scenario.name}-", dir=base) as directory:
            try:
                result = run_scenario(scenario, Path(directory))
                invariant = "pass"
                try:
                    assert_invariants(result)
                except AssertionError:
                    invariant = "fail"
                leads = sum(result.outcome_counts.values())
                outcomes = ", ".join(f"{key}={value}" for key, value in sorted(result.outcome_counts.items())) or "-"
                counted = sum(result.attempts_per_lead.values())
                uncounted = sum(result.uncounted_attempts_per_lead.values())
                rows.append("| " + " | ".join(map(_cell, (
                    scenario.name, result.state.value, leads, outcomes, counted, uncounted,
                    result.model_calls, f"{result.wall_time:.3f}s", f"{result.peak_memory} B", invariant,
                ))) + " |")
            except Exception as error:
                rows.append("| " + " | ".join(map(_cell, (
                    scenario.name, f"error: {type(error).__name__}", "-", "-", "-", "-", "-",
                    f"{perf_counter() - started:.3f}s", "-", "fail",
                ))) + " |")
            finally:
                if result is not None:
                    result.store.close()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "| scenario | final state | leads | outcomes per status | counted attempts | uncounted attempts | "
        "model calls | wall time | peak memory | invariants |\n"
        "| --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | --- |\n"
        + "\n".join(rows) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def render(scenarios: Iterable[Scenario], output: Path) -> None:
    render_report(scenarios, output)


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {Path(sys.argv[0]).name} OUTPUT.md", file=sys.stderr)
        return 2
    render_report(SCENARIOS, Path(sys.argv[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
