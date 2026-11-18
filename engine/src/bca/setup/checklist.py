from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Callable, Iterator, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, cast

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
        fresh = [
            ChecklistItem(name, "manual_pending" if name in MANUAL else "pending", "", f"setup.{name}")
            for name in NAMES
        ]
        if not self.path.is_file():
            return fresh
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                raise ValueError("state is not a list")
            saved = {
                item["name"]: item for item in raw if isinstance(item, dict) and item.get("name") in NAMES
            }
            for item in fresh:
                previous = saved.get(item.name)
                if previous is None:
                    continue
                status = previous.get("status")
                valid = status in {"pending", "ok", "failed"} or (
                    item.name in MANUAL and status in {"manual_pending", "manual_done"}
                )
                if valid:
                    item.status = cast(Status, status)
                    detail = previous.get("detail", "")
                    item.detail = detail if isinstance(detail, str) else ""
            return fresh
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            corrupt = self.path.with_suffix(self.path.suffix + ".corrupt")
            with contextlib.suppress(OSError):
                os.replace(self.path, corrupt)
            return fresh

    @contextlib.contextmanager
    def _lock(self) -> Iterator[None]:
        lock = self.path.with_suffix(self.path.suffix + ".lock")
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_RDWR)
        try:
            yield
        finally:
            os.close(fd)
            lock.unlink(missing_ok=True)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=self.path.parent, prefix=".checklist-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump([asdict(item) for item in self.items], handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.path)
        finally:
            Path(name).unlink(missing_ok=True)

    def run_checks(self, probes: Mapping[str, Callable[[], tuple[bool, str]]]) -> list[ChecklistItem]:
        results: dict[str, tuple[Status, str]] = {}
        for name, probe in probes.items():
            if name in MANUAL or name not in NAMES:
                continue
            try:
                ok, detail = probe()
                results[name] = ("ok" if ok else "failed", detail)
            except Exception as exc:
                results[name] = ("failed", f"probe failed: {exc}")
        with self._lock():
            self.items = self._load()
            for item in self.items:
                if item.name in results:
                    item.status, item.detail = results[item.name]
                elif item.name not in MANUAL and item.name not in probes:
                    item.status, item.detail = "failed", "probe not supplied"
            self.save()
            return self.items

    def mark_manual(self, name: str, done: bool = True, detail: str = "operator confirmed") -> None:
        if name not in MANUAL:
            raise ValueError(f"not a manual checklist item: {name}")
        with self._lock():
            self.items = self._load()
            item = next(item for item in self.items if item.name == name)
            item.status, item.detail = ("manual_done" if done else "manual_pending"), detail
            self.save()
