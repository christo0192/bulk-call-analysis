from __future__ import annotations

from pathlib import Path

SCRIPTS = (
    Path(__file__).parents[2] / "scripts" / "run-app.ps1",
    Path(__file__).parents[2] / "scripts" / "run-app.sh",
)


def test_run_app_scripts_do_not_touch_credentials() -> None:
    forbidden = ("credentials", "auth.json", ".env", "api_key")
    for path in SCRIPTS:
        text = path.read_text(encoding="utf-8").lower()
        assert not any(word in text for word in forbidden), path


def test_run_app_scripts_use_the_stdio_entry_point() -> None:
    for path in SCRIPTS:
        text = path.read_text(encoding="utf-8")
        assert "BCA_ENGINE_ARGS" in text
        assert '"-m","bca.api.stdio"' in text or '["-m","bca.api.stdio"]' in text


def test_run_app_scripts_install_spacy() -> None:
    # spaCy is the optional nlp extra; a plain `uv sync` leaves it out and the first job fails.
    for path in SCRIPTS:
        text = path.read_text(encoding="utf-8")
        assert "uv sync --extra nlp" in text, path
        assert "ensure_model" in text, path
