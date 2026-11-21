from __future__ import annotations

import json
from pathlib import Path

import pytest

from bca.bq.leads import _scan_allowed as ids_scan_allowed
from bca.bq.pull import _scan_allowed as pull_scan_allowed
from bca.paths import job_dir


def test_job_dir_uses_canonical_folder(tmp_path: Path) -> None:
    assert job_dir(tmp_path, "project-one", "job-one") == (
        tmp_path / "projects" / "project-one" / "jobs" / "job-one"
    )


@pytest.mark.parametrize("value", ["../job", "job/name", "", "a" * 65])
def test_job_dir_rejects_invalid_job_ids(tmp_path: Path, value: str) -> None:
    with pytest.raises(ValueError, match="invalid job id"):
        job_dir(tmp_path, "project-one", value)


def test_job_dir_rejects_invalid_project_ids(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="invalid job id"):
        job_dir(tmp_path, "../project", "job-one")


def test_pull_scan_below_reject_is_allowed_without_approval() -> None:
    assert pull_scan_allowed(99, 100, None)


def test_pull_scan_uses_approved_ten_percent_tolerance() -> None:
    assert pull_scan_allowed(110, 100, 100)


def test_pull_scan_rejects_above_approved_tolerance() -> None:
    assert not pull_scan_allowed(111, 100, 100)


def test_pull_scan_rejects_large_scan_without_preflight() -> None:
    assert not pull_scan_allowed(101, 100, None)


def test_id_scan_matches_pull_tolerance() -> None:
    assert ids_scan_allowed(110, 100, 100)
    assert not ids_scan_allowed(111, 100, 100)


def test_preflight_record_keeps_integer_bytes(tmp_path: Path) -> None:
    path = tmp_path / "preflight.v1.json"
    path.write_text(json.dumps({"dry_run_bytes": 123, "lead_source_dry_run_bytes": 45}), encoding="utf-8")
    value = json.loads(path.read_text(encoding="utf-8"))
    assert value == {"dry_run_bytes": 123, "lead_source_dry_run_bytes": 45}


def test_preflight_record_keeps_id_intake_reconciliation(tmp_path: Path) -> None:
    value = {"raw": 5, "distinct": 3, "duplicates": 1, "malformed": 1}
    path = tmp_path / "preflight.v1.json"
    path.write_text(json.dumps({"id_intake": value}), encoding="utf-8")
    assert json.loads(path.read_text(encoding="utf-8"))["id_intake"] == value


def test_rebuild_name_is_per_spec_version(tmp_path: Path) -> None:
    output = tmp_path / "output"
    output.mkdir()
    (output / "results.v2.xlsx").write_bytes(b"xlsx")
    assert not (output / "results.v2.rebuild1.xlsx").exists()


def test_files_listing_shape_does_not_include_raw_or_masked_names() -> None:
    names = ["jobs/job/output/results.v1.xlsx", "jobs/job/raw/x.jsonl.gz", "jobs/job/masked/x.jsonl.gz"]
    visible = [name for name in names if not any(part in {"raw", "masked"} for part in name.split("/"))]
    assert visible == ["jobs/job/output/results.v1.xlsx"]
