from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field

from bca.bq.runner import DryRun


@dataclass
class FakeBqRunner:
    """Deterministic runner for query orchestration tests; it never contacts BigQuery."""

    pages: list[list[dict[str, object]]] = field(default_factory=list)
    dry: DryRun = field(default_factory=lambda: DryRun(0, [], []))
    error: BaseException | None = None
    calls: list[tuple[str, Mapping[str, object] | None, int]] = field(default_factory=list)

    def dry_run(self, sql: str, params: Mapping[str, object] | None = None) -> DryRun:
        self.calls.append((sql, params, 0))
        if self.error is not None:
            raise self.error
        return self.dry

    def iter_pages(
        self, sql: str, params: Mapping[str, object] | None = None, page_size: int = 1000
    ) -> Iterator[list[dict[str, object]]]:
        self.calls.append((sql, params, page_size))
        if self.error is not None:
            raise self.error
        yield from self.pages
