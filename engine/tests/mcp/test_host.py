from __future__ import annotations

import json
import re
import socket
import sys
import threading
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, cast

import pytest

from bca.bq.runner import DryRun
from bca.config import BigQueryConfig, ModelsConfig, RecipeConfig
from bca.logsetup import setup_logging
from bca.mask.masker import MaskingContext
from bca.mask.names import Span
from bca.mcp.host import (
    MAX_RESULT_CHARS,
    ToolHost,
    ToolHostError,
    start_ipc_thread,
)
from bca.mcp.ipc import call_engine, new_ipc_address
from bca.mcp.wire import HANDSHAKE_TIMEOUT, OK, expected_mac
from bca.store import LeadStatus, open_store
from tests.fakes.fake_bq import FakeBqRunner


class FakeNer:
    def persons(self, text: str) -> list[Span]:
        return []


def recipe_config() -> RecipeConfig:
    return RecipeConfig(
        transcripts_table="dataset.transcripts",
        calls_table="dataset.calls",
        lead_id_column="lead_id",
        call_id_column="call_id",
        transcript_text_column="transcript_text",
        call_start_column="call_start",
        call_duration_column="duration",
        speaker_labels={"learner": ("learner",), "advisor": ("advisor",)},
    )


def make_host(
    tmp_path: Path,
    runner: FakeBqRunner | None = None,
    *,
    recipe: RecipeConfig | None = None,
    models: ModelsConfig | None = None,
    notes_path: Path | None = None,
) -> ToolHost:
    tmp_path.mkdir(parents=True, exist_ok=True)
    store = open_store(tmp_path / "store.sqlite")
    store.create_project("project", "Project")
    return ToolHost(
        runner=runner or FakeBqRunner(),
        store=store,
        project_dir=tmp_path,
        bq_config=BigQueryConfig("project", reject_bytes=100),
        mask_context=MaskingContext(ner=FakeNer()),
        project_id="project",
        recipe=recipe,
        models=models,
        notes_path=notes_path,
    )


def test_bq_query_masks_phone_numbers(tmp_path: Path) -> None:
    runner = FakeBqRunner(pages=[[{"phone": "Call 9876543210"}]])
    result = make_host(tmp_path, runner).call("bq_query", {"sql": "SELECT 1"})
    assert result == {"bytes": "0 B", "rows": [{"phone": "Call [PHONE]"}], "truncated": False}
    assert runner.calls[-1][2] == 500


def test_bq_query_stops_a_bounded_page_producer(tmp_path: Path) -> None:
    released = threading.Event()

    class CountingRunner(FakeBqRunner):
        yielded_after_consumer_return = 0
        producer_thread: threading.Thread | None = None

        def iter_pages(
            self,
            sql: str,
            params: Mapping[str, object] | None = None,
            page_size: int = 1000,
            timeout: float | None = None,
        ) -> Iterator[list[dict[str, object]]]:
            self.producer_thread = threading.current_thread()
            for index in range(100):
                if released.is_set():
                    self.yielded_after_consumer_return += 1
                yield [{"value": index}]

    runner = CountingRunner()
    result = make_host(tmp_path, runner).call("bq_query", {"sql": "SELECT 1", "max_rows": 1})
    assert result["truncated"] is True  # type: ignore[index]
    assert result["more_rows"] is True  # type: ignore[index]
    assert "aggregate in sql or narrow the query" in str(result["hint"]).lower()  # type: ignore[index]
    released.set()
    assert runner.producer_thread is not None
    runner.producer_thread.join(timeout=2)
    assert not runner.producer_thread.is_alive()
    assert runner.yielded_after_consumer_return <= 3


@pytest.mark.parametrize(
    "value, expected",
    [(float("nan"), "NaN"), (float("inf"), "Infinity"), (float("-inf"), "-Infinity")],
)
def test_bq_query_serializes_non_finite_floats(tmp_path: Path, value: float, expected: str) -> None:
    result = make_host(tmp_path, FakeBqRunner(pages=[[{"value": value}]])).call(
        "bq_query", {"sql": "SELECT 1"}
    )
    assert result["rows"] == [{"value": expected}]  # type: ignore[index]


@pytest.mark.parametrize(
    "value, expected",
    [
        (9876543210.5, "[NUMBER]"),
        (-9876543210.5, "[NUMBER]"),
        (1234567890123.9, "[NUMBER]"),
        (98765432.5, 98765432.5),
        (3.25, 3.25),
    ],
)
def test_bq_query_masks_only_sensitive_float_sizes(tmp_path: Path, value: float, expected: object) -> None:
    result = make_host(tmp_path, FakeBqRunner(pages=[[{"value": value}]])).call(
        "bq_query", {"sql": "SELECT 1"}
    )
    assert result["rows"] == [{"value": expected}]  # type: ignore[index]


def test_bq_query_deadline_cancels_a_blocking_runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cancelled = threading.Event()

    class BlockingRunner(FakeBqRunner):
        def cancel(self) -> None:
            cancelled.set()

        def iter_pages(self, *args: Any, **kwargs: Any) -> Iterator[list[dict[str, object]]]:
            time.sleep(4)
            yield []

    monkeypatch.setattr("bca.mcp.host.BQ_QUERY_DEADLINE_S", 1.0)
    host = make_host(tmp_path, BlockingRunner())
    started = time.monotonic()
    with pytest.raises(ToolHostError, match="timed out"):
        host.call("bq_query", {"sql": "SELECT 1"})
    elapsed = time.monotonic() - started
    assert elapsed <= 1.5
    assert cancelled.is_set()


def test_string_results_are_plain_text_with_original_character_count(tmp_path: Path) -> None:
    notes = tmp_path / "notes.md"
    short = "line one\nline two"
    notes.write_text(short, encoding="utf-8")
    host = make_host(tmp_path, notes_path=notes)
    assert host.call("data_notes") == short

    original = "a\n" * (MAX_RESULT_CHARS // 2 + 10)
    notes.write_text(original, encoding="utf-8")
    result = host.call("data_notes")
    assert isinstance(result, str)
    assert "\\n" not in result
    match = re.search(r"\[\.\.\. (\d+) characters omitted \.\.\.\]$", result)
    assert match is not None
    prefix = result[: match.start()]
    assert int(match.group(1)) == len(original) - (len(prefix) - 1)


def test_result_cap_has_marker(tmp_path: Path) -> None:
    runner = FakeBqRunner(pages=[[{"text": "x" * 5000}] for _ in range(20)])
    result = make_host(tmp_path, runner).call("bq_query", {"sql": "SELECT 1", "max_rows": 1000})
    encoded = json.dumps(result, default=str)
    assert len(encoded) <= MAX_RESULT_CHARS + 100
    assert "rows omitted" in encoded


def test_large_scan_is_refused_without_confirm_parameter(tmp_path: Path) -> None:
    runner = FakeBqRunner(dry=DryRun(101, [], []))
    with pytest.raises(ToolHostError, match="too large for chat; propose it as the job's lead source"):
        make_host(tmp_path, runner).call("bq_query", {"sql": "SELECT 1"})


def test_write_file_rejects_traversal_and_other_names(tmp_path: Path) -> None:
    host = make_host(tmp_path)
    for name in ("../plan.md", "notes.md", "plan.md/child"):
        with pytest.raises(ToolHostError):
            host.call("write_file", {"name": name, "text": "x"})


def test_job_ids_are_confined_before_store_or_filesystem_access(tmp_path: Path) -> None:
    host = make_host(tmp_path)
    for job_id in ("/absolute", "..", r"C:\outside", r"\\server\share\job"):
        with pytest.raises(ToolHostError, match="invalid job id"):
            host.call("job_status", {"job_id": job_id})


def test_host_requires_an_explicit_real_masking_context(tmp_path: Path) -> None:
    store = open_store(tmp_path / "store.sqlite")
    with pytest.raises(TypeError):
        ToolHost(  # type: ignore[call-arg]
            runner=FakeBqRunner(), store=store, project_dir=tmp_path, bq_config=BigQueryConfig("project")
        )
    with pytest.raises(ValueError, match="real masking NER"):
        ToolHost(
            runner=FakeBqRunner(),
            store=store,
            project_dir=tmp_path,
            bq_config=BigQueryConfig("project"),
            mask_context=MaskingContext(),
        )


def test_nested_keys_and_bytes_are_masked(tmp_path: Path) -> None:
    runner = FakeBqRunner(pages=[[{"x@example.com 9876543210": b"x@example.com"}]])
    result = make_host(tmp_path, runner).call("bq_query", {"sql": "SELECT 1"})
    assert result["rows"][0] == {"[EMAIL] [PHONE]": "[BYTES 13]"}  # type: ignore[index]


def test_lead_transcript_rejects_non_sample_lead(tmp_path: Path) -> None:
    host = make_host(tmp_path)
    host.store.create_job("job", "project", 1)
    host.store.add_lead("job", "lead", LeadStatus.done, in_sample=0)
    with pytest.raises(ToolHostError, match="sample reference"):
        host.call("lead_transcript", {"job_id": "job", "ref": "S1"})


def test_ipc_forwards_call_and_does_not_log_authkey(tmp_path: Path) -> None:
    host = make_host(tmp_path)
    logger = setup_logging(tmp_path)
    address, family = new_ipc_address()
    authkey = b"a" * 32
    thread = start_ipc_thread(host, address, authkey)
    try:
        result = call_engine(address, authkey, "data_notes", {}, family)
        assert result == "no notes yet"
    finally:
        host.close()
        for handler in logger.handlers:
            handler.flush()
            handler.close()
    log_file = tmp_path / "engine.log"
    log_text = log_file.read_text(encoding="utf-8") if log_file.exists() else ""
    assert authkey.hex() not in log_text
    assert not thread.is_alive()


def test_ipc_close_without_client_stops_thread(tmp_path: Path) -> None:
    host = make_host(tmp_path)
    address, _family = new_ipc_address()
    thread = start_ipc_thread(host, address, b"b" * 32)
    try:
        started = time.monotonic()
        host.close()
        assert time.monotonic() - started < 5
    finally:
        host.close()
    assert not thread.is_alive()


def test_ipc_close_twice_is_safe(tmp_path: Path) -> None:
    host = make_host(tmp_path)
    address, _family = new_ipc_address()
    thread = start_ipc_thread(host, address, b"d" * 32)
    try:
        host.close()
        host.close()
    finally:
        host.close()
    assert not thread.is_alive()


def test_ipc_client_disconnect_does_not_kill_listener(tmp_path: Path) -> None:
    host = make_host(tmp_path)
    address, family = new_ipc_address()
    authkey = b"c" * 32
    thread = start_ipc_thread(host, address, authkey)
    try:
        from multiprocessing.connection import Client

        connection = Client(address, family=family, authkey=None)
        assert connection.poll(5)
        hello = connection.recv_bytes(64)
        connection.send_bytes(expected_mac(authkey, hello))
        assert connection.poll(5)
        assert connection.recv_bytes(8) == OK
        connection.send_bytes(b'{"tool":"data_notes","arguments":{}}')
        connection.close()
        time.sleep(0.1)
        assert call_engine(address, authkey, "data_notes", {}, family) == "no notes yet"
    finally:
        host.close()
    assert not thread.is_alive()


def test_wrong_authkey_is_rejected_and_listener_survives(tmp_path: Path) -> None:
    host = make_host(tmp_path)
    address, family = new_ipc_address()
    authkey = b"w" * 32
    thread = start_ipc_thread(host, address, authkey)
    try:
        with pytest.raises(ToolHostError, match="rejected"):
            call_engine(address, b"wrong", "data_notes", {}, family)
        assert call_engine(address, authkey, "data_notes", {}, family) == "no notes yet"
    finally:
        host.close()
    assert not thread.is_alive()


def test_ipc_close_with_silent_client_returns_quickly(tmp_path: Path) -> None:
    host = make_host(tmp_path)
    address, family = new_ipc_address()
    authkey = b"e" * 32
    thread = start_ipc_thread(host, address, authkey)
    silent: Any
    if family == "AF_UNIX":
        silent = socket.socket(cast(int, socket.__dict__["AF_UNIX"]), socket.SOCK_STREAM)
    elif sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        handle = kernel32.CreateFileW(address, 0xC0000000, 0, None, 3, 0, None)
        if handle == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        silent = (kernel32, handle)
    else:
        raise RuntimeError(f"unsupported IPC family: {family}")
    try:
        if family == "AF_UNIX":
            silent.connect(address)
        started = time.monotonic()
        assert call_engine(address, authkey, "data_notes", {}, family) == "no notes yet"
        assert time.monotonic() - started < 5
    finally:
        if family == "AF_UNIX":
            silent.close()
        else:
            silent[0].CloseHandle(silent[1])
        started = time.monotonic()
        host.close()
        assert time.monotonic() - started < 5
    assert not thread.is_alive()


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="raw bounded-client test is POSIX-only")
def test_ipc_failed_handshakes_do_not_create_unbounded_threads(tmp_path: Path) -> None:
    host = make_host(tmp_path)
    address, family = new_ipc_address()
    authkey = b"h" * 32
    thread = start_ipc_thread(host, address, authkey)
    baseline = threading.active_count()
    all_clients: list[socket.socket] = []
    try:
        unix_family = cast(int, socket.__dict__["AF_UNIX"])
        for wave_start in range(0, 50, 8):
            wave: list[socket.socket] = []
            for _ in range(min(8, 50 - wave_start)):
                client = socket.socket(unix_family, socket.SOCK_STREAM)
                all_clients.append(client)
                client.connect(address)
                wave.append(client)
            time.sleep(HANDSHAKE_TIMEOUT + 0.1)
            assert threading.active_count() <= baseline + 8
            for client in wave:
                client.close()
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline and threading.active_count() > baseline + 8:
                time.sleep(0.01)
            assert threading.active_count() <= baseline + 8

        started = time.monotonic()
        assert call_engine(address, authkey, "data_notes", {}, family) == "no notes yet"
        assert time.monotonic() - started < 5
    finally:
        for client in all_clients:
            client.close()
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
