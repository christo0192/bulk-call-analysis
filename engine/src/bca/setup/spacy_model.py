from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

MODEL_WHEEL_URL = "https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl#sha256=1932429db727d4bff3deed6b34cfc05df17794f4a52eeb26cf8928f7c1a0fb85"


class SpacyModelError(RuntimeError):
    pass


def ensure_model(*, run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> bool:
    if importlib.util.find_spec("en_core_web_sm") is not None:
        return False
    uv = shutil.which("uv")
    if uv is None:
        raise SpacyModelError("could not find the uv executable")
    command = [str(Path(uv).resolve()), "pip", "install", "--python", sys.executable, MODEL_WHEEL_URL]
    try:
        result = run(command, check=False, text=True)
    except OSError as exc:
        raise SpacyModelError(f"could not install spaCy model with uv: {exc}") from exc
    if result.returncode != 0:
        raise SpacyModelError("uv could not install the pinned spaCy model wheel")
    return True
