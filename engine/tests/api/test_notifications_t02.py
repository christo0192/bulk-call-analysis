from __future__ import annotations

import json
import math
from pathlib import Path

from bca.api.server import strict_json_value
from bca.mask.names import Span, mask_names
from bca.monitor.eta import Eta
from bca.monitor.progress import counts
from bca.store import JobState, LeadStatus, open_store
from bca.store.lifecycle import public_state


class TaggedNer:
    def persons(self, text: str) -> list[Span]:
        start = text.find("Synthetic Person")
        return [] if start < 0 else [Span(start, start + len("Synthetic Person"))]


def test_strict_json_replaces_nested_infinity() -> None:
    value = strict_json_value({"a": [math.inf, {"b": math.nan}]}, __import__("logging").getLogger())
    assert value == {"a": [None, {"b": None}]}


def test_strict_json_output_has_no_nonfinite_constants() -> None:
    value = strict_json_value({"x": float("-inf")}, __import__("logging").getLogger())
    encoded = json.dumps(value, allow_nan=False)
    assert json.loads(encoded, parse_constant=lambda _: (_ for _ in ()).throw(ValueError())) == {"x": None}


def test_eta_unknown_is_publicly_null() -> None:
    assert Eta(math.inf, math.inf).public() == {"low": None, "high": None, "seeded": False, "known": False}


def test_eta_known_has_numeric_bounds() -> None:
    assert Eta(1.0, 2.0, True).public() == {"low": 1.0, "high": 2.0, "seeded": True, "known": True}


def test_public_state_planned_mapping() -> None:
    assert public_state("draft") == "planned"
    assert public_state("plan_proposed") == "planned"


def test_public_state_running_mapping() -> None:
    assert public_state("sample_running") == "running"
    assert public_state("verifying") == "running"


def test_public_state_terminal_mapping() -> None:
    assert public_state("done_with_issues") == "done_with_issues"
    assert public_state("cancelled") == "cancelled"


def test_name_masking_ner_precedes_greeting_rule() -> None:
    assert mask_names("Hello Synthetic Person", None, TaggedNer(), ())[0] == "Hello [NAME]"


def test_name_masking_ner_precedes_introduction_rule() -> None:
    assert mask_names("My name is Synthetic Person", None, TaggedNer(), ())[0] == "My name is [NAME]"


def test_name_masking_overlapping_spans_make_one_placeholder() -> None:
    result, counts_by_detector = mask_names("Hello Synthetic Person", None, TaggedNer(), ())
    assert result.count("[NAME]") == 1 and counts_by_detector["NER"] == 1


def test_name_masking_roster_still_uses_advisor_placeholder() -> None:
    import re

    result, counts_by_detector = mask_names(
        "Synthetic Person called", re.compile("Synthetic Person"), None, ()
    )
    assert result == "[ADVISOR] called" and counts_by_detector["ROSTER"] == 1


def test_sample_counts_scope_reconciles() -> None:
    store = open_store(Path(":memory:"))
    store.create_project("project", "Project")
    store.create_job("job", "project", 1, JobState.sample_running)
    for lead, status in (("a", LeadStatus.done), ("b", LeadStatus.in_flight), ("c", LeadStatus.queued)):
        store.add_lead("job", lead, status, in_sample=1)
    result = counts(store, "job", sample=True)
    assert (
        result.total
        == result.done
        + result.in_flight
        + result.retrying
        + sum(result.failed.values())
        + result.held_back
        + result.remaining
    )


def test_full_counts_exclude_sample_scope() -> None:
    store = open_store(Path(":memory:"))
    store.create_project("project", "Project")
    store.create_job("job", "project", 1, JobState.full_running)
    store.add_lead("job", "sample", LeadStatus.done, in_sample=1)
    store.add_lead("job", "full", LeadStatus.queued, in_sample=0)
    assert counts(store, "job", sample=False).total == 1


def test_counts_held_back_is_in_identity() -> None:
    store = open_store(Path(":memory:"))
    store.create_project("project", "Project")
    store.create_job("job", "project", 1, JobState.full_running)
    store.add_lead("job", "held", LeadStatus.held_back, in_sample=0)
    result = counts(store, "job", sample=False)
    assert result.total == 1 and result.held_back == 1 and result.remaining == 0


def test_counts_failed_is_in_identity() -> None:
    store = open_store(Path(":memory:"))
    store.create_project("project", "Project")
    store.create_job("job", "project", 1, JobState.full_running)
    store.add_lead("job", "failed", LeadStatus.failed, in_sample=0)
    result = counts(store, "job", sample=False)
    assert result.total == 1 and sum(result.failed.values()) == 1 and result.remaining == 0
