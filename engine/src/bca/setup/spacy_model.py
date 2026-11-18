from __future__ import annotations

import importlib.util
import subprocess
import sys
from collections.abc import Callable

MODEL_WHEEL_URL = "https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.7.1/en_core_web_sm-3.7.1-py3-none-any.whl"


class SpacyModelError(RuntimeError):
    pass


def ensure_model(*, run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> bool:
    if importlib.util.find_spec("en_core_web_sm") is not None:
        return False
    command = [sys.executable, "-m", "uv", "pip", "install", MODEL_WHEEL_URL]
    try:
        result = run(command, check=False, text=True)
    except OSError as exc:
        raise SpacyModelError(f"could not install spaCy model with uv: {exc}") from exc
    if result.returncode != 0:
        raise SpacyModelError("uv could not install the pinned spaCy model wheel")
    return True
