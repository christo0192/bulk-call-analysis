"""Record fields that are masked and leak-scanned."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

METADATA_FIELDS = frozenset({"lead_id", "call_id", "call_start", "duration", "transcript_status"})


def _map_free_text(value: Any, transform: Callable[[str], str]) -> Any:
    if isinstance(value, dict):
        return {key: _map_free_text(item, transform) for key, item in value.items()}
    if isinstance(value, list):
        return [_map_free_text(item, transform) for item in value]
    if isinstance(value, str):
        return transform(value)
    return value


def map_free_text_values(record: dict[str, Any], transform: Callable[[str], str]) -> dict[str, Any]:
    """Apply ``transform`` to every free-text string in a record."""
    return {
        key: value if key in METADATA_FIELDS else _map_free_text(value, transform)
        for key, value in record.items()
    }


def free_text_values(record: dict[str, Any]) -> Iterator[str]:
    """Everything except these metadata keys is treated as free text and is masked and leak-scanned."""
    values: list[str] = []

    def collect(value: str) -> str:
        values.append(value)
        return value

    map_free_text_values(record, collect)
    yield from values
