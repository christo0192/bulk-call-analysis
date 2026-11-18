from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

Status = Literal["pending", "ok", "failed", "manual_pending", "manual_done"]
NAMES = (
    "uv_python",
    "venv",
    "spacy",
    "connector",
    "key",
    "claude_copy",
    "codex_copy",
    "claude_login",
    "chatgpt_login",
    "training_off",
    "extra_usage_off",
)
MANUAL = frozenset({"training_off", "extra_usage_off"})


@dataclass
class ChecklistItem:
    name: str
    status: Status
    detail: str
    action_id: str


class Checklist:
    def __init__(self, app_data: Path) -> None:
        self.path = app_data / "setup-checklist.json"
        self.items = self._load()

    def _load(self) -> list[ChecklistItem]:
        if not self.path.is_file():
            return [
                ChecklistItem(name, "manual_pending" if name in MANUAL else "pending", "", f"setup.{name}")
                for name in NAMES
            ]
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return [ChecklistItem(**item) for item in data]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=self.path.parent, prefix=".checklist-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump([asdict(item) for item in self.items], handle, indent=2)
                handle.write("\n")
            os.replace(name, self.path)
        finally:
            Path(name).unlink(missing_ok=True)

    def run_checks(self, probes: Mapping[str, Callable[[], tuple[bool, str]]]) -> list[ChecklistItem]:
        for item in self.items:
            if item.name in MANUAL:
                continue
            probe = probes.get(item.name)
            if probe is None:
                item.status, item.detail = "failed", "probe not supplied"
                continue
            try:
                ok, detail = probe()
            except Exception as exc:
                item.status, item.detail = "failed", f"probe failed: {exc}"
                continue
            item.status, item.detail = ("ok" if ok else "failed"), detail
        self.save()
        return self.items

    def mark_manual(self, name: str, done: bool = True, detail: str = "operator confirmed") -> None:
        if name not in MANUAL:
            raise ValueError(f"not a manual checklist item: {name}")
        item = next(item for item in self.items if item.name == name)
        item.status, item.detail = ("manual_done" if done else "manual_pending"), detail
        self.save()
