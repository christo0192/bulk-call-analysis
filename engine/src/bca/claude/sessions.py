from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4


@dataclass(frozen=True)
class SessionChoice:
    status: str
    session: UUID | None = None


class SessionStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _read(self) -> dict[str, dict[str, str]]:
        if not self.path.exists():
            return {}
        value = json.loads(self.path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}

    def _write(self, value: dict[str, dict[str, str]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")

    def get(self, project: str, playbook_version: str) -> SessionChoice:
        item = self._read().get(project)
        if item is None:
            return SessionChoice("new", None)
        if item.get("playbook_version") != playbook_version:
            return SessionChoice("needs_choice", UUID(item["session"]))
        return SessionChoice("resume", UUID(item["session"]))

    def save(self, project: str, session: UUID, playbook_version: str) -> None:
        value = self._read()
        value[project] = {"session": str(session), "playbook_version": playbook_version}
        self._write(value)

    def new(self, project: str, playbook_version: str) -> UUID:
        session = uuid4()
        self.save(project, session, playbook_version)
        return session

    def reseed(self, project: str, playbook_version: str) -> UUID:
        return self.new(project, playbook_version)

    def resume_or_reseed(
        self, project: str, playbook_version: str, conversation_exists: bool
    ) -> SessionChoice:
        choice = self.get(project, playbook_version)
        if choice.status == "resume" and not conversation_exists:
            return SessionChoice("new", self.reseed(project, playbook_version))
        return choice
