from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from bca.bq.runner import DryRun
from bca.config import BigQueryConfig
from bca.mask.masker import MaskingContext
from bca.mcp.host import MAX_RESULT_CHARS, ToolHost, ToolHostError, start_ipc_thread
from bca.mcp.ipc import call_engine, new_ipc_address
from bca.store import LeadStatus, open_store
from tests.fakes.fake_bq import FakeBqRunner


def make_host(tmp_path: Path, runner: FakeBqRunner | None = None) -> ToolHost:
    store = open_store(tmp_path / "store.sqlite")
    store.create_project("project", "Project")
    return ToolHost(
        runner=runner or FakeBqRunner(),
        store=store,
        project_dir=tmp_path,
        bq_config=BigQueryConfig("project", reject_bytes=100),
        mask_context=MaskingContext(),
    )


def test_bq_query_masks_phone_numbers(tmp_path: Path) -> None:
    runner = FakeBqRunner(pages=[[{"phone": "Call 9876543210"}]])
    result = make_host(tmp_path, runner).call("bq_query", {"sql": "SELECT 1"})
    assert result == {"bytes": 0, "rows": [{"phone": "Call [PHONE]"}], "truncated": False}


def test_result_cap_has_marker(tmp_path: Path) -> None:
    runner = FakeBqRunner(pages=[[{"text": "x" * 5000}] for _ in range(20)])
    result = make_host(tmp_path, runner).call("bq_query", {"sql": "SELECT 1", "max_rows": 1000})
    encoded = json.dumps(result, default=str)
    assert len(encoded) <= MAX_RESULT_CHARS + 100
    assert "rows omitted" in encoded


def test_large_scan_is_refused_without_confirm_parameter(tmp_path: Path) -> None:
    runner = FakeBqRunner(dry=DryRun(101, [], []))
    with pytest.raises(ToolHostError, match="unexpected keyword argument 'confirm'"):
        make_host(tmp_path, runner).call("bq_query", {"sql": "SELECT 1", "confirm": True})


def test_write_file_rejects_traversal_and_other_names(tmp_path: Path) -> None:
    host = make_host(tmp_path)
    for name in ("../plan.md", "notes.md", "plan.md/child"):
        with pytest.raises(ToolHostError):
            host.call("write_file", {"name": name, "text": "x"})


def test_lead_transcript_rejects_non_sample_lead(tmp_path: Path) -> None:
    host = make_host(tmp_path)
    host.store.create_job("job", "project", 1)
    host.store.add_lead("job", "lead", LeadStatus.done, in_sample=0)
    with pytest.raises(ToolHostError, match="sample lead"):
        host.call("lead_transcript", {"job_id": "job", "lead_id": "lead"})


def test_ipc_forwards_call_and_does_not_log_authkey(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    host = make_host(tmp_path)
    address, family = new_ipc_address()
    authkey = b"a" * 32
    thread = start_ipc_thread(host, address, authkey)
    try:
        result = call_engine(address, authkey, "data_notes", {}, family)
        assert result == "no notes yet"
        assert authkey.hex() not in caplog.text
    finally:
        host.close()
    assert not thread.is_alive()


def test_ipc_close_without_client_stops_thread(tmp_path: Path) -> None:
    host = make_host(tmp_path)
    address, _family = new_ipc_address()
    thread = start_ipc_thread(host, address, b"b" * 32)
    started = time.monotonic()
    host.close()
    assert time.monotonic() - started < 5
    assert not thread.is_alive()


def test_new_ipc_address_is_private_and_uses_posix_socket_on_posix() -> None:
    address, family = new_ipc_address()
    if family == "AF_UNIX":
        socket_dir = Path(address).parent
        assert socket_dir.stat().st_mode & 0o777 == 0o700
        socket_dir.rmdir()
    else:
        assert address.startswith(r"\\.\pipe\bca-")
        assert len(address.rsplit("-", 1)[1]) == 32
