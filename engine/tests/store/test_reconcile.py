from pathlib import Path

from bca.store import LeadStatus, open_store, reconcile
from bca.store.reconcile import reconcile_rows


def test_reconciliation_reports_all_imbalances() -> None:
    store = open_store(Path(":memory:"))
    store.create_project("p", "Project")
    store.create_job("j", "p", 1)
    store.add_lead("j", "one", LeadStatus.queued, calls_pulled=2, calls_sent=1, calls_dropped_cap=0)
    store.add_lead("j", "two", LeadStatus.queued, calls_pulled=1, calls_sent=1, calls_dropped_cap=0)
    store.connection.execute("INSERT INTO id_intake VALUES ('j', 6, 4, 1, 0)")
    result = reconcile_rows(
        [("one", "queued", 2, 1, 0), ("two", "queued", 1, 1, 0), ("two", "queued", 0, 0, 0)],
        [("two", 2)],
        (6, 4, 1, 0),
    )
    assert not result.ok
    assert any("distinct ids" in problem for problem in result.problems)
    assert "call identity mismatch for one" in result.problems
    assert "duplicate lead rows" in result.problems
    assert "id intake counts do not reconcile" in result.problems
    assert "| Status | Count |" in result.to_markdown()


def test_consistent_reconciliation_is_ok() -> None:
    store = open_store(Path(":memory:"))
    store.create_project("p", "Project")
    store.create_job("j", "p", 1)
    store.add_lead("j", "one", LeadStatus.queued, calls_pulled=2, calls_sent=1, calls_dropped_cap=1)
    attempt = store.start_attempt("j", "one", "model")
    assert store.record_result("j", "one", attempt, {}, True, [])
    store.connection.execute("INSERT INTO id_intake VALUES ('j', 1, 1, 0, 0)")
    result = reconcile(store, "j")
    assert result.ok
    assert "| done | 1 |" in result.to_markdown()
