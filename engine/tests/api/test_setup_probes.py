"""The first-run checklist's details must say what the result is, in plain words.

The live app showed "configured" next to a FAILED masking row and a failed BigQuery row, because
the probes returned the same word for success and failure.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from typing import Any, cast

import pytest

from bca.api.production import ProductionBackend
from bca.store import open_store
from tests.api.test_production import make_backend


def _items(backend: ProductionBackend) -> dict[str, dict[str, Any]]:
    result = cast(dict[str, Any], asyncio.run(backend.call("setup/runChecks", {})))
    return {item["name"]: item for item in result["items"]}


def test_setup_details_match_their_result_without_settings(tmp_path: Path) -> None:
    # No site.toml anywhere this backend looks, so it starts without settings.
    backend = ProductionBackend(tmp_path, config=None, store=open_store(Path(":memory:")))
    assert backend.config is None
    items = _items(backend)
    assert items["connector"]["status"] == "failed"
    assert items["connector"]["detail"].startswith("BigQuery settings not found; put site.toml in ")
    assert items["key"]["status"] == "failed"
    assert items["key"]["detail"] == "Waiting for the BigQuery settings."
    for item in items.values():
        # A failed row never says something that reads like success.
        if item["status"] == "failed":
            assert item["detail"] not in {"configured", "located", "codex", "available"}


def test_missing_key_file_names_the_path(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    items = _items(backend)
    assert items["connector"]["status"] == "ok"
    assert items["connector"]["detail"] == "BigQuery settings found."
    assert items["key"]["status"] == "failed"
    assert items["key"]["detail"].startswith("The BigQuery key file is missing: ")
    assert str(tmp_path / "connector.json") in items["key"]["detail"]


def test_key_file_present(tmp_path: Path) -> None:
    (tmp_path / "connector.json").write_text("{}", encoding="utf-8")
    items = _items(make_backend(tmp_path))
    assert items["key"] == {**items["key"], "status": "ok", "detail": "BigQuery key file found."}


@pytest.mark.parametrize(
    ("spacy_installed", "model_installed", "status", "detail"),
    [
        (True, True, "ok", "Name-masking model installed."),
        # The model wheel does not depend on spaCy: the model alone must not read as ready.
        (False, True, "failed", "spaCy is not installed. Run the app's start script again."),
        (True, False, "failed", "The name-masking model is not installed. Run the app's start script again."),
        (False, False, "failed", "spaCy is not installed. Run the app's start script again."),
    ],
)
def test_name_model_row_needs_spacy_and_the_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spacy_installed: bool,
    model_installed: bool,
    status: str,
    detail: str,
) -> None:
    real_find_spec = importlib.util.find_spec
    present = {"spacy": spacy_installed, "en_core_web_sm": model_installed}

    def find_spec(name: str, *args: Any, **kwargs: Any) -> object:
        if name in present:
            return object() if present[name] else None
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", find_spec)
    item = _items(make_backend(tmp_path))["spacy"]
    assert (item["status"], item["detail"]) == (status, detail)


def test_key_path_that_is_a_folder_says_so(tmp_path: Path) -> None:
    (tmp_path / "connector.json").mkdir()
    item = _items(make_backend(tmp_path))["key"]
    assert item["status"] == "failed"
    assert item["detail"].startswith("The BigQuery key path is a folder, not a file: ")


def test_unreadable_settings_file_is_not_called_missing(tmp_path: Path) -> None:
    backend = ProductionBackend(tmp_path, config=None, store=open_store(Path(":memory:")))
    backend.config_path.parent.mkdir(parents=True, exist_ok=True)
    backend.config_path.write_text("this is [not valid toml", encoding="utf-8")
    item = _items(backend)["connector"]
    assert item["status"] == "failed"
    assert item["detail"].startswith(f"The settings file {backend.config_path} could not be read: ")


@pytest.mark.parametrize("found", [True, False])
def test_codex_row_says_whether_the_program_was_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, found: bool
) -> None:
    monkeypatch.setattr("bca.api.production.shutil.which", lambda name: "codex" if found else None)
    item = _items(make_backend(tmp_path))["codex_copy"]
    assert item["status"] == ("ok" if found else "failed")
    assert item["detail"] == (
        "Codex program found."
        if found
        else "The Codex program was not found. Install it, then press Check again."
    )
