# Testing

The simulator drives the real coordinator, store, masking, output, and fake model
boundaries with synthetic leads and transcripts. It does not contact a model, a
vendor account, or BigQuery.

The scenario list comes from the names in `tests/sim/test_scenarios.py`: small-happy,
medium-happy, mixed-populations, id-list-quality, quota-reset-canary, credit-pause,
explicit-reroute, auth-recovery, model-recovery, error-storm-recovery,
operator-pause, sleep-requeue, two-sleep-gaps, crash-sample, crash-full,
crash-output, crash-repair, crash-after-recover, leak-holdbacks, quote-repair,
quote-repair-fails, schema-retry-exhausted, transient-retry, error-storm-pause,
rebuild-rules, rebuild-twice, stale-plan-gate, stale-sample-gate, stale-full-gate,
automated-plan-gate, automated-sample-gate, automated-full-gate, slow-liveness,
long-stall, and stdio-api.

The invariants check that reconciliation passes, the verified workbook exists and
contains no formulas, and that every scenario reaches its expected final state.
Fake Codex records every turn prompt in the scenario working directory; the
invariants collect every planted phone, email, PAN, and name belonging to a
lead that reached a prompt and reject any of those tokens. PII leads must be
masked and complete, while the separate synthetic leak and masker failure
remain held back for their persisted reasons. The simulator also checks
scenario-specific retry, pause, sleep-gap, recovery, gate, and failure
expectations. Crash-after-recover performs two recoveries and verifies that
results are not duplicated; long-stall hangs one fake turn and verifies the
product's short lead-timeout failure (or a successful retry), not a fabricated
liveness event.

From `engine/`, run the fast suite with `uv run pytest -q -m 'not slow'`.
Run the slow suite with `uv run pytest -q -m slow`. Run lint with
`uv run ruff check .` and `uv run ruff format --check .`, and type checking with
`uv run mypy`.

Safety tests are in `engine/tests/safety`. They verify the child environment,
configuration isolation, billing guard, initialization guard, and orphan
handling. The orphan test uses the engine's process-tree termination helper on
POSIX and a kill-on-close Windows job assigned before the parent spawns its
children; on Windows it verifies that closing the kill-on-close job terminates the
parent and both descendants. They use only fakes and synthetic data.

To measure memory, run `python scripts/footprint.py --pid PID --interval 5
--duration 600 --out footprint.csv`. For a synthetic in-process run, use
`python scripts/footprint.py --sim 20`; it prints the simulator peak and the
steady process-tree private working set.

One live smoke test remains: run the 20-lead job with official subscription
logins on Windows and on macOS. That test is intentionally not part of CI.
