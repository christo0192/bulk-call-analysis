from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from bca.store.spec import QuoteField

Speaker = Literal["learner", "advisor", "any"]


@dataclass(frozen=True)
class QuoteCheck:
    ok: bool
    reason: str
    diag: str = ""
    field: str = ""
    call_number: int | None = None


def _normalise(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    for old, new in (("‘", "'"), ("’", "'"), ("“", '"'), ("”", '"'), ("–", "-"), ("—", "-")):  # noqa: RUF001
        value = value.replace(old, new)
    return re.sub(r"\s+", " ", value).strip().casefold()


def check_quote(
    quote: str, call_text: str, speaker: Speaker, speaker_labels: Mapping[str, list[str] | tuple[str, ...]]
) -> QuoteCheck:
    expected = _normalise(quote)
    lines: list[tuple[str, str]] = []
    current: tuple[str, str] | None = None
    for raw in call_text.splitlines():
        match = re.match(r"\s*(?:\[\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?\]\s*)?([^:]+):\s*(.*)", raw)
        if match:
            current = (_normalise(match.group(1)), _normalise(match.group(2)))
            lines.append(current)
        elif current and raw.strip():
            current = (current[0], _normalise(current[1] + " " + raw))
            lines[-1] = current
    allowed = (
        {_normalise(label) for label in speaker_labels.get(speaker, ())}
        if speaker != "any"
        else {_normalise(label) for labels in speaker_labels.values() for label in labels}
    )

    def contains(text: str) -> bool:
        return bool(expected and re.search(rf"(?<!\w){re.escape(expected)}(?!\w)", text))

    if any(label in allowed and contains(text) for label, text in lines):
        return QuoteCheck(True, "exact match")
    all_text = " ".join(text for _, text in lines)
    if contains(all_text) and speaker != "any":
        return QuoteCheck(False, "wrong speaker", "quote found under another speaker")
    if expected:
        from difflib import SequenceMatcher

        ratio = max((SequenceMatcher(None, expected, text).ratio() for _, text in lines), default=0.0)
        if ratio >= 0.85:
            return QuoteCheck(False, "near miss", f"fuzzy ratio={ratio:.3f}")
    return QuoteCheck(False, "not found", "no exact normalised substring")


def _call_section(bundle: str, number: int) -> str:
    marker = re.compile(r"^=== CALL (\d+) of (\d+)\b.*$", re.MULTILINE)
    matches = list(marker.finditer(bundle))
    for index, match in enumerate(matches):
        if int(match.group(1)) == number:
            end = matches[index + 1].start() if index + 1 < len(matches) else len(bundle)
            return bundle[match.end() : end]
    return ""


def check_answer(
    answer: Mapping[str, object],
    quote_fields: tuple[QuoteField, ...],
    bundle_text: str,
    speaker_labels: Mapping[str, list[str] | tuple[str, ...]] | None = None,
) -> list[QuoteCheck]:
    labels = speaker_labels or {
        "learner": ["Customer", "Lead", "Learner"],
        "advisor": ["Agent", "Advisor", "PA"],
    }
    raw_call = answer.get("call_number")
    if raw_call is None:
        call: int | None = None
    elif isinstance(raw_call, bool):
        call = -1
    elif isinstance(raw_call, int):
        call = raw_call
    elif (isinstance(raw_call, float) and raw_call.is_integer()) or (
        isinstance(raw_call, str) and re.fullmatch(r"[0-9]+", raw_call) is not None
    ):
        call = int(raw_call)
    else:
        call = -1
    section = bundle_text if call is None else _call_section(bundle_text, call)
    checks: list[QuoteCheck] = []
    for field in quote_fields:
        value = answer.get(field.field)
        check = (
            check_quote(value, section, field.speaker, labels)
            if isinstance(value, str)
            else QuoteCheck(False, "not found", f"{field.field} is not a string")
        )
        checks.append(QuoteCheck(check.ok, check.reason, check.diag, field.field, call))
    if call == -1:
        return [
            QuoteCheck(False, "bad call number", "call_number must be an integer", item.field, call)
            for item in checks
        ]
    return checks


def repair_prompt(answer: Mapping[str, object], failures: list[QuoteCheck], instructions: str) -> str:
    details = "; ".join(
        f"field={item.field}, call={item.call_number}, quote={answer.get(item.field)!r}, "
        f"reason={item.reason}: {item.diag}"
        for item in failures
    )
    previous = json.dumps(answer, ensure_ascii=False, sort_keys=True)
    return (
        f"{instructions}\nRepair only the failed quote fields. Return the complete answer JSON, "
        f"preserving all valid values. Previous answer JSON: {previous}. Diagnostics: {details}"
    )
