from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_engine_starts_and_reports_missing_site_config() -> None:
    temp_root = Path(os.environ.get("TMP", Path.cwd() / ".pytest_tmp_root"))
    data_dir = temp_root / f"startup-data-{os.getpid()}"
    _run_startup(data_dir)


def _run_startup(data_dir: Path) -> None:
    environment = os.environ.copy()
    environment["BCA_DATA_DIR"] = str(data_dir)
    home = data_dir.parent / "home"
    home.mkdir(parents=True, exist_ok=True)
    environment["HOME"] = str(home)
    environment["USERPROFILE"] = str(home)
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "app/status", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "setup/checklist", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "shutdown", "params": {}},
    ]
    process = subprocess.Popen(
        [sys.executable, "-m", "bca.api.stdio"],
        cwd=Path(__file__).parents[2],
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    input_text = "".join(json.dumps(request) + "\n" for request in requests)
    try:
        stdout, stderr = process.communicate(input_text, timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        raise AssertionError(f"stdio engine did not stop: {stderr}") from None
    assert process.returncode == 0, stderr
    responses = [json.loads(line) for line in stdout.splitlines()]
    assert responses[0]["id"] == 1
    assert responses[1]["id"] == 2
    assert responses[0]["result"]["state"] == "ready"
    assert responses[0]["result"]["protocol"] == "2.0"
    checklist = responses[1]["result"]
    connector = next(item for item in checklist if item["name"] == "connector")
    assert connector["detail"].startswith("BigQuery settings not found")
